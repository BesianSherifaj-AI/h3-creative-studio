"""Prepare editable H3 API and UI workflows against one explicit Comfy renderer."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import mimetypes
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path

import httpx
from PIL import Image

from . import media
from vendor.prompt import comfy_transfer as transfer
from vendor.prompt.mmh3_transfer import repair_studio_controls
from vendor.prompt.video_timing import frame_budget
from vendor.prompt.resources import local_url


def timing(duration, overlap=0):
    settings = {"duration_basis": "new_footage", "experimental_preview": duration == 3,
                "continuation_source": bool(overlap), "continuation_overlap_frames": overlap}
    budget = frame_budget(duration, settings)
    frames = int(duration) * 24
    return {"delivery_frames": frames, "render_frames": budget["frames"], "first": overlap,
            "last": overlap + frames - 1, "trim": [overlap, overlap + frames], "fps": 24}


def guide_layout(frame, frames, timeline, source_size=None, target_size=None, audio_seconds=None):
    if type(frame) is not int or not 0 <= frame < timeline["delivery_frames"]:
        raise ValueError("Place the guide inside the delivered shot.")
    if type(frames) is not int or frames < 1:
        raise ValueError("A visual guide needs at least one decoded frame.")
    retained = 1 if frames < 5 else 5 + 17 * ((frames - 5) // 17)
    if frame + retained > timeline["delivery_frames"]:
        raise ValueError(f"The retained {retained}-frame guide crosses the end of the delivered shot.")
    result = {"frame": frame, "frame_idx": timeline["first"] + frame, "input_frames": frames,
              "retained_frames": retained, "crop": "center"}
    if source_size and target_size:
        w, h = source_size
        tw, th = target_size
        old_aspect, new_aspect = w / h, tw / th
        x = round((w - w * (new_aspect / old_aspect)) / 2) if old_aspect > new_aspect else 0
        y = round((h - h * (old_aspect / new_aspect)) / 2) if old_aspect < new_aspect else 0
        result["source_crop_xywh"] = [x, y, w - 2*x, h - 2*y]
    if audio_seconds is not None:
        # H3's joint latent has a rounded 40-Hz audio grid and floors the slice end.
        ticks = math.floor(round(timeline["render_frames"] / 24 * 40) - (5/3) * result["frame_idx"])
        if ticks < 1:
            raise ValueError("This frame is past the end of the H3 audio track.")
        result["available_audio_ticks"] = ticks
        result["render_audio_seconds"] = min(audio_seconds, ticks / 40)
        result["delivered_audio_seconds"] = min(audio_seconds, (timeline["delivery_frames"] - frame) / 24)
    return result


def _probe_guide(path, kind, with_audio):
    if kind == "image":
        with Image.open(path) as image:
            image.verify()
            return dict(frames=1, dimensions=list(image.size), audio_seconds=None)
    if kind not in ("audio", "video"):
        raise ValueError("Choose an image, video or audio guide.")
    formats = media.AUDIO_FORMATS if kind == "audio" else media._FORMATS
    args = ["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-format_whitelist", formats, "-count_frames", "-show_streams", "-show_format", "-of", "json", str(path)]
    info = json.loads(subprocess.run(args, capture_output=True, check=True, timeout=120,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout)
    streams = info["streams"]
    audio = next((s for s in streams if s["codec_type"] == "audio"), None)
    has_audio = kind == "audio" or with_audio
    if has_audio and not audio:
        raise ValueError("The guide has no selected audio track.")
    seconds = float(audio.get("duration") or info["format"]["duration"]) if has_audio else None
    if kind == "audio":
        return dict(frames=1, dimensions=None, audio_seconds=seconds)
    video = next((s for s in streams if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    if not video:
        raise ValueError("The guide has no video stream.")
    meta = media.probe_media(path)
    origin = meta["start_pts"] * Fraction(meta["video_time_base"])
    if has_audio and audio.get("start_time") and audio.get("duration"):
        seconds = max(0., float(Fraction(audio["start_time"]) - origin) + float(audio["duration"]))
    return dict(frames=max(1, math.ceil(Fraction(meta["duration_exact"]) * 24)),
                dimensions=[meta["width"], meta["height"]], audio_seconds=seconds,
                source_frames=int(video["nb_read_frames"]), source_rate=meta["fps"],
                source_duration=meta["duration_exact"], source_time_base=meta["video_time_base"],
                source_start_pts=meta["start_pts"], guide_rate=24)


def _conform_video(source, destination, guide, layout):
    # Sample the picture held at each H3 tick. FFmpeg autorotates into display coordinates.
    origin = guide["source_start_pts"] * Fraction(guide["source_time_base"])
    offset = media._seconds(origin)
    filters = (f"setpts=PTS-({offset})/TB,fps=24:start_time=0:round=up:eof_action=pass,"
               f"tpad=stop_mode=clone:stop=-1,trim=end_frame={guide['frames']},setpts=N/(24*TB)")
    args = [*media._ffmpeg(), "-copyts", *media._INPUT, "-i", str(source), "-map", "0:V:0", "-vf", filters,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "0", "-threads", "2"]
    if guide.get("with_audio"):
        audio = (f"asetpts=PTS-({offset})/TB,aresample=48000:async=1:first_pts=0,"
                 f"atrim=end_sample={round(layout['delivered_audio_seconds'] * 48000)}")
        args.extend(["-map", "0:a:0", "-af", audio, "-c:a", "alac"])
    else:
        args.append("-an")
    media._run([*args, "-map_metadata", "-1", str(destination)])


def _add_guides(bundle, guides, schema, timeline):
    graph = bundle["prompt"]
    manifest = bundle["manifest"]
    def node(kind):
        return next(item for item in graph.values() if item["class_type"] == kind)["inputs"]
    def add(kind, inputs, title):
        ident = str(max(map(int, graph)) + 1)
        graph[ident] = {"class_type": kind, "inputs": inputs, "_meta": {"title": title, "benchmark_group": "Conditioning"}}
        return [ident, 0]
    guider = node("BasicGuider")
    latent = node("SamplerCustomAdvanced")["latent_image"]
    vae, audio_vae = node("VAEDecode")["vae"], node("VAEDecodeAudio")["vae"]
    target = [manifest["width"], manifest["height"]]
    receipts = []
    # The native endpoint is the padded generation's last frame. Move this
    # conditioning input to the last frame actually retained in the edit.
    cond = graph[manifest["conditioning_node_id"]]["inputs"]
    endpoints = []
    for role, moved, frame in (("first_frame", timeline["first"] != 0, 0),
                               ("last_frame", timeline["last"] != timeline["render_frames"] - 1, timeline["delivery_frames"] - 1)):
        if moved and role in cond:
            ref = next(ref for ref in manifest["references"] if ref["role"] == role)
            endpoints.append({"kind": "image", "asset_id": ref["asset_id"], "frame": frame,
                              "revision": ref.get("revision"), "sha256": ref["sha256"], "dimensions": ref["dimensions"],
                              "frames": 1, "link": cond.pop(role), "role": role})
    guides = [*endpoints, *guides]
    for guide in guides:
        kind = guide["kind"]
        layout = guide_layout(guide["frame"], guide["frames"], timeline, guide.get("dimensions"), target, guide.get("audio_seconds"))
        link = guide.get("link")
        if link is None:
            loader, field = {"image": ("LoadImage", "image"), "video": ("LoadVideo", "file"),
                             "audio": ("LoadAudio", "audio")}[kind]
            link = add(loader, {field: guide["filename"]}, "Timeline " + kind)
        inputs = {"positive": guider["conditioning"], "latent": latent, "frame_idx": layout["frame_idx"]}
        if kind == "video":
            link = add("GetVideoComponents", {"video": link}, "Guide clip frames and sound")
        if kind != "audio":
            inputs.update(image=link, vae=vae)
        if kind == "audio" or guide.get("with_audio"):
            inputs.update(audio=link if kind == "audio" else [link[0], 1], audio_vae=audio_vae)
        guider["conditioning"] = add("MiniMaxH3AddGuide", inputs, f"{kind.title()} guide · frame {guide['frame']}")
        receipts.append({**{k: guide.get(k) for k in ("asset_id", "revision", "sha256", "kind", "role", "with_audio", "filename")},
                         **{k: guide[k] for k in ("source_frames","source_rate","source_duration","source_time_base","source_start_pts","source_sha256","guide_rate") if k in guide},
                         **layout, "node_id": guider["conditioning"][0]})
    manifest.update(timeline=timeline, guides=receipts)
    names = {n["inputs"][field] for n in graph.values() for loader, field in
             (("LoadImage", "image"), ("LoadVideo", "file"), ("LoadAudio", "audio")) if n["class_type"] == loader}
    transfer._materialize_ui_defaults(graph, schema)
    transfer._validate_graph(graph, schema, names)
    bundle["workflow"] = transfer._ui_workflow(graph, schema, bundle["id"], "H3 Creative Studio")
    bundle["workflow"]["extra"]["h3_prompt_studio"] = copy.deepcopy(manifest)


def restore_packet(base, path, parent, client):
    """Restore only a hash- and identity-matched archived motion packet."""
    base = local_url(base)
    path = Path(path).resolve(strict=True)
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if not parent.get("sha256") or digest != parent["sha256"]:
        raise ValueError("The archived motion packet does not match the take's SHA-256.")
    subfolder = "h3_creative_studio/restored/" + digest[:16]
    name = digest + ".mmh3"
    with path.open("rb") as source:
        response = client.post(base + "/upload/image", files={"image": (name, source, "application/octet-stream")},
                               data={"type": "output", "subfolder": subfolder, "overwrite": "false"}, timeout=180)
        response.raise_for_status()
    result = response.json()
    if result.get("name") != name or result.get("type") != "output" or result.get("subfolder", "").replace(chr(92), "/") != subfolder:
        raise ValueError("The renderer did not restore the packet at its expected location.")
    verify = hashlib.sha256()
    with client.stream("GET", base + "/view", params={"type": "output", "subfolder": subfolder, "filename": name}, timeout=180) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes(1024 * 1024):
            verify.update(chunk)
    if verify.hexdigest() != digest:
        raise ValueError("The restored packet bytes differ from the archive.")
    selector = "output::" + subfolder + "/" + name
    info = client.get(base + "/mmh3_media/file_info", params={"file": selector}, timeout=30)
    info.raise_for_status()
    info = info.json()
    if not info.get("has", {}).get("latent") or info.get("id") != parent["packet_id"] or not info.get("revision"):
        raise ValueError("The archived motion packet has a different identity or no renderer revision.")
    # MMH3's card revision is mtime+size; transferring identical contents
    # creates a new token. The full SHA-256 and packet ID prove content identity.
    return selector, info["revision"]


def _verify_input(client, base, name, digest):
    subfolder, _, filename = name.rpartition("/")
    actual = hashlib.sha256()
    with client.stream("GET", base + "/view", params={"filename": filename, "subfolder": subfolder, "type": "input"}, timeout=180) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes(1024 * 1024):
            actual.update(chunk)
    if actual.hexdigest() != digest:
        raise ValueError("The stored guide or reference changed on the renderer.")


def prepare_take(spec, resolve_asset, *, client=None):
    """resolve_asset receives a library ID and returns its immutable media path."""
    if client is None:
        with httpx.Client(trust_env=False, follow_redirects=False) as owned:
            return prepare_take(spec, resolve_asset, client=owned)
    base = spec["comfy_url"].rstrip("/")
    mode = spec.get("mode", "t2va")
    settings = {**spec.get("settings", {}), "seed": spec.get("seed", 0),
                "comfy_url": base, "comfy_urls": [base], "duration_basis": "new_footage"}
    if "loras_by_mode" in settings:
        settings["loras"] = settings["loras_by_mode"].get(mode)
    parent = spec.get("parent")
    if parent:
        if any(parent.get(key) is None for key in ("take_id", "packet_id", "packet_revision", "selector", "trim")):
            raise ValueError("A continuation needs its exact parent take, packet, revision and displayed trim.")
        if parent.get("ending_edited") or parent.get("render_frames", parent["trim"][1]) != parent["trim"][1]:
            if not parent.get("use_original_motion"):
                raise ValueError("The saved packet ends differently from this cut. Choose original motion, a visual start or regenerate.")
        selector, live_revision = parent["selector"], parent["packet_revision"]
        if parent.get("packet_asset_id"):
            # A different installation or restored file can change both the
            # selector and its mtime token. Never accept an unrelated packet.
            _, schema = transfer._connect(settings, client)
            from vendor.prompt.mmh3_transfer import mmh3_options
            choices = {r["selector"] for r in mmh3_options(schema)["mmh3_sources"]}
            selector = "output::" + selector.removeprefix("output::")
            can_reuse = False
            if selector in choices:
                card = client.get(base + "/mmh3_media/file_info", params={"file": selector}, timeout=30)
                card.raise_for_status()
                info = card.json()
                can_reuse = info.get("id") == parent["packet_id"] and info.get("revision") == live_revision
            if not can_reuse:
                selector, live_revision = restore_packet(base, resolve_asset(parent["packet_asset_id"]), parent, client)
        settings.update(continuation_source=selector, continuation_overlap_frames=parent.get("overlap", 39))
    duration = spec.get("duration", 5)
    timeline = timing(duration, settings.get("continuation_overlap_frames", 0) if parent else 0)
    if settings.get("align_motion_ending"):
        if parent:
            raise ValueError("Regenerate from the displayed image to align a new motion ending.")
        offset = timeline["render_frames"] - timeline["delivery_frames"]
        timeline.update(first=offset, last=timeline["render_frames"] - 1, trim=[offset, timeline["render_frames"]],
                        align_motion_ending=True)
    settings["experimental_preview"] = duration == 3 or bool(settings.get("experimental_preview"))
    project = {"id": spec.get("project_id"), "title": spec.get("title", "Shot"), "mode": mode,
               "duration": duration, "assets": spec.get("assets", [])}
    bundle = transfer.build_transfer(project, spec["prompt"], settings, lambda asset: resolve_asset(asset["id"]), client=client)
    manifest = bundle["manifest"]
    manifest["parent"] = copy.deepcopy(parent)
    if parent:
        source = manifest["mmh3"]
        if source["source_packet_id"] != parent["packet_id"] or source["source_revision"] != live_revision:
            raise ValueError("The renderer's packet identity or revision differs from the selected take.")
    revisions = {asset["id"]: asset.get("revision") for asset in project["assets"]}
    for ref in manifest["references"]:
        ref["revision"] = revisions.get(ref["asset_id"])
    _, schema = transfer._connect(settings, client)
    guides, uploads = [], []
    subfolder = "h3_creative_studio/" + bundle["id"]
    with tempfile.TemporaryDirectory() as temp:
        for index, request in enumerate(spec.get("guides", [])):
            kind = request.get("kind", "image")
            path = Path(resolve_asset(request["asset_id"])).resolve(strict=True)
            if not 0 < path.stat().st_size <= 256 * 1024 * 1024:
                raise ValueError("Timed guides must fit within 256 MB each.")
            guide = {**request, "kind": kind, **_probe_guide(path, kind, request.get("with_audio", False))}
            with path.open("rb") as stream:
                guide["source_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
            name = f"guide-{index}" + path.suffix.lower()
            digest = guide["source_sha256"]
            layout = guide_layout(guide["frame"], guide["frames"], timeline, audio_seconds=guide["audio_seconds"])
            if kind == "video":
                name = f"guide-{index}-24fps.mp4"
                conformed = Path(temp) / name
                _conform_video(path, conformed, guide, layout)
                path = conformed
            elif kind == "audio" and guide["audio_seconds"] > layout["delivered_audio_seconds"]:
                name = f"guide-{index}-cut.wav"
                conformed = Path(temp) / name
                samples = round(layout["delivered_audio_seconds"] * 48000)
                media._run([*media._ffmpeg(), "-protocol_whitelist", "file,pipe", "-format_whitelist", media.AUDIO_FORMATS,
                            "-i", str(path), "-map", "0:a:0", "-af", f"aresample=48000,atrim=end_sample={samples}",
                            "-c:a", "pcm_s24le", str(conformed)])
                path = conformed
            if path.parent == Path(temp):
                with path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
            guides.append({**guide, "filename": subfolder + "/" + name, "sha256": digest})
            uploads.append((name, path, digest))
        _add_guides(bundle, guides, schema, timeline)
        for name, path, digest in uploads:
            with path.open("rb") as stream:
                reply = client.post(base + "/upload/image",
                                    files={"image": (name, stream, mimetypes.guess_type(name)[0] or "application/octet-stream")},
                                    data={"type": "input", "subfolder": subfolder, "overwrite": "false"}, timeout=180)
            reply.raise_for_status()
            result = reply.json()
            if result.get("type") != "input" or result.get("name") != name or result.get("subfolder", "").replace(chr(92), "/") != subfolder:
                raise ValueError("The renderer stored the guide at an unexpected location.")
            _verify_input(client, base, subfolder + "/" + name, digest)
    return bundle


def validate_saved_take(bundle, *, client=None):
    """Check dependencies and the branch of every timed guide after reopening."""
    if client is None:
        with httpx.Client(trust_env=False) as owned:
            return validate_saved_take(bundle, client=owned)
    base = bundle["comfy_url"]
    _, schema = transfer._connect({"comfy_urls": [base]}, client)
    graph, manifest = bundle["prompt"], bundle["manifest"]
    motion = manifest.get("mmh3", {})
    if motion.get("source"):
        selector = "output::" + motion["source"].removeprefix("output::")
        card = client.get(base + "/mmh3_media/file_info", params={"file": selector}, timeout=30)
        card.raise_for_status()
        current = card.json()
        if (current.get("id") != motion.get("source_packet_id")
                or current.get("revision") != motion.get("source_revision") or not current.get("has", {}).get("latent")):
            raise ValueError("The parent packet changed after preparing this take. Restore the saved parent and prepare again.")
    if motion.get("save_enabled"):
        repair_studio_controls(graph, schema, manifest)
    # LoadImage's list omits files in subfolders on some Comfy builds.
    # Validate recorded media bytes separately and still require every model.
    names = {r["comfy_file"]: r["sha256"] for r in manifest.get("references", [])}
    for guide in manifest.get("guides", []):
        if guide.get("filename"):
            names[guide["filename"]] = guide["sha256"]
    transfer._validate_graph(graph, schema, set(names))
    for name, digest in names.items():
        _verify_input(client, base, name, digest)
    return True
