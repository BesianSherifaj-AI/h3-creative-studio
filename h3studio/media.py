"""Source-timestamp media I/O and CPU delivery for H3 Creative Studio.

Trims are half-open seconds relative to the first displayed video frame.
Source-frame edit keys are integers in the original video stream time base.
"""
from __future__ import annotations

from contextlib import contextmanager
from fractions import Fraction
from pathlib import Path
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid

from PIL import Image, ImageOps

MAX_FRAME_PIXELS = 33_554_432
_FORMATS = "mov,matroska,avi,mpegts,mpeg,asf,flv,ogg,nut"
AUDIO_FORMATS = _FORMATS + ",wav,mp3,flac,aac,aiff,ac3,eac3,au,caf,w64,tta,wv"
_INPUT = ["-protocol_whitelist", "file,pipe", "-format_whitelist", _FORMATS]
_HIDDEN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _bin(name):
    executable = shutil.which(name)
    if not executable:
        raise ValueError(f"Install FFmpeg and add {name} to PATH.")
    return executable


def _ffmpeg():
    return [_bin("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-filter_threads", "1", "-filter_complex_threads", "1"]


def _run(args, cancel=None):
    # A file avoids deadlock even if a decoder emits a large number of errors.
    with tempfile.TemporaryFile() as error:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=error, creationflags=_HIDDEN)
        try:
            while process.poll() is None:
                if cancel and cancel():
                    raise InterruptedError("Media rendering cancelled.")
                time.sleep(.06)
            if process.returncode:
                error.seek(max(0, error.tell() - 3000))
                raise ValueError(error.read().decode("utf-8", "replace").strip() or "FFmpeg failed.")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def _probe(path, args):
    source = Path(path).resolve(strict=True)
    result = subprocess.run([_bin("ffprobe"), "-v", "error", *_INPUT, *args,
                             "-of", "json", str(source)], capture_output=True, creationflags=_HIDDEN)
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", "replace")[-2000:])
    return json.loads(result.stdout)


def audio_stream_count(path):
    result = subprocess.run([_bin("ffprobe"), "-v", "error", "-protocol_whitelist", "file,pipe",
                             "-format_whitelist", AUDIO_FORMATS, "-select_streams", "a",
                             "-show_entries", "stream=index", "-of", "csv=p=0", str(Path(path).resolve(strict=True))],
                            capture_output=True, creationflags=_HIDDEN)
    if result.returncode or not result.stdout.strip():
        raise ValueError("Choose a soundtrack file containing decodable audio.")
    return len(result.stdout.strip().splitlines())


def _ratio(value):
    try:
        return Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        raise ValueError("Expected a finite number or rational fraction.") from None


def _seconds(value):
    return f"{float(value):.12f}"


def _positive_rate(value):
    rate = _ratio(value)
    if not 0 < rate <= 240:
        raise ValueError("Delivery frame rate must be greater than zero and at most 240.")
    return rate


def _dimensions(width, height):
    if (type(width) is not int or type(height) is not int or min(width, height) < 2
            or width % 2 or height % 2 or width * height > MAX_FRAME_PIXELS):
        raise ValueError(f"Use even delivery dimensions up to {MAX_FRAME_PIXELS:,} pixels per frame.")


def _video(raw):
    return next((s for s in raw["streams"] if s["codec_type"] == "video"
                 and not s.get("disposition", {}).get("attached_pic")), None)


def probe_media(path):
    raw = _probe(path, ["-show_streams", "-show_format"])
    video = _video(raw)
    if not video:
        raise ValueError("This file has no moving-picture stream.")
    w, h = video["width"], video["height"]
    if w * h > MAX_FRAME_PIXELS:
        raise ValueError(f"Source exceeds the {MAX_FRAME_PIXELS:,}-pixel frame limit.")
    rotation = next((int(round(float(x["rotation"]))) % 360 for x in video.get("side_data_list", [])
                     if "rotation" in x), int(video.get("tags", {}).get("rotate", 0)) % 360)
    if rotation not in (0, 90, 180, 270):
        raise ValueError("Editing supports right-angle source rotations.")
    base = _ratio(video["time_base"])
    origin = int(video.get("start_pts") or 0)
    if "duration_ts" in video:
        duration = int(video["duration_ts"]) * base
    elif "duration" in video:
        duration = _ratio(video["duration"])
    elif "DURATION" in video.get("tags", {}):
        hms = video["tags"]["DURATION"].split(":")
        duration = 3600 * int(hms[0]) + 60 * int(hms[1]) + _ratio(hms[2]) - origin * base
    else:
        # Some containers (notably NUT) report the last packet's start as their
        # duration. Audio length can also differ from picture length.
        args = [_bin("ffprobe"), "-v", "error", *_INPUT, "-select_streams", str(video["index"]),
                "-show_packets", "-show_entries", "packet=pts,duration", "-of", "csv=p=0",
                str(Path(path).resolve())]
        rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0"
        try:
            guessed_duration = 1 / _ratio(rate)
        except (ValueError, ZeroDivisionError):
            guessed_duration = base
        end = origin * base
        with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              encoding="utf-8", creationflags=_HIDDEN) as scan:
            for line in scan.stdout:
                fields = line.strip().split(",")
                if len(fields) >= 2 and fields[0].lstrip("-").isdigit():
                    span = int(fields[1])*base if fields[1].isdigit() and int(fields[1]) else guessed_duration
                    end = max(end, int(fields[0])*base + span)
            if scan.wait():
                raise ValueError("Could not read this video's presentation times.")
        duration = end - origin * base
    # NUT preserves the source time base but carries no native colorspace fields.
    # Studio FFV1 masters record the encoded RGB transfer and primaries on the stream.
    tags = {k.lower():v for k,v in video.get("tags", {}).items()}
    transfer = video.get("color_transfer", "unknown")
    primaries = video.get("color_primaries", "unknown")
    if video.get("codec_name") == "ffv1" and video.get("pix_fmt") == "gbrp16le":
        candidate = tags.get("h3_transfer")
        if candidate in {"bt709","smpte170m","smpte240m","linear","iec61966-2-1","bt2020-10","bt2020-12","smpte2084","arib-std-b67"}:
            transfer = candidate
        candidate = tags.get("h3_primaries")
        if candidate in {"bt709","bt470m","bt470bg","smpte170m","smpte240m","film","bt2020","smpte431","smpte432"}:
            primaries = candidate
    return dict(width=h if rotation % 180 else w, height=w if rotation % 180 else h,
                coded_width=w, coded_height=h, rotation=rotation, duration=float(duration),
                start_pts=origin, duration_exact=str(duration), video_time_base=str(base),
                video_stream=video["index"], fps=video.get("avg_frame_rate") or video["r_frame_rate"],
                video_codec=video.get("codec_name"), pix_fmt=video.get("pix_fmt"), color_transfer=transfer,
                color_space=video.get("color_space", "unknown"), color_range=video.get("color_range", "unknown"),
                color_primaries=primaries,
                audio_streams=[dict(index=s["index"], channels=s.get("channels", 0),
                                    sample_rate=s.get("sample_rate"), codec=s.get("codec_name"),
                                    title=s.get("tags", {}).get("title", "")) for s in raw["streams"]
                               if s["codec_type"] == "audio"])


def frame_index(path):
    meta = probe_media(path)
    base = _ratio(meta["video_time_base"])
    result = _probe(path, ["-select_streams", str(meta["video_stream"]), "-show_frames",
                           "-show_entries", "frame=best_effort_timestamp,duration"])
    frames = []
    for index, f in enumerate(result["frames"]):
        if "best_effort_timestamp" not in f:
            raise ValueError("A decoded frame has no presentation timestamp.")
        pts = int(f["best_effort_timestamp"])
        if frames and pts <= frames[-1]["pts"]:
            raise ValueError("Source frame timestamps must increase to address edits unambiguously.")
        frames.append(dict(index=index, pts=pts, time_seconds=float((pts - meta["start_pts"]) * base)))
    return frames


@contextmanager
def _output(target, inputs=()):
    path = Path(target).resolve()
    if path in {Path(p).resolve() for p in inputs}:
        raise ValueError("Choose a different output from the source.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.stem}-{uuid.uuid4().hex}{path.suffix}")
    try:
        yield temp
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def extract_frame(path, pts, output):
    if type(pts) is not int:
        raise ValueError("Choose an integer source PTS from the frame index.")
    meta = probe_media(path)
    with _output(output, [path]) as temp:
        _run([*_ffmpeg(), "-copyts", *_INPUT, "-i", str(Path(path).resolve()), "-map",
              f"0:{meta['video_stream']}", "-vf", f"select=eq(pts\\,{pts})",
              "-frames:v", "1", "-fps_mode", "passthrough", "-c:v", "png",
              "-pix_fmt", "rgb48be", "-threads", "2", "-f", "image2", str(temp)])
        if not temp.is_file():
            raise ValueError("No source frame has that presentation timestamp.")
    return {**meta, "pts": pts, "path": str(Path(output).resolve()), "precision": "rgb48"}


def _delivery_color(meta, target):
    pq = target == "bt2020-pq"
    pix_fmt = "yuv420p10le" if pq else "yuv420p"
    source_hdr = meta["color_transfer"] in ("smpte2084", "arib-std-b67")
    rgb = (meta["pix_fmt"] or "").startswith(("rgb", "bgr", "gbr"))
    # Untagged HD footage defaults to BT.709, and untagged RGB uses sRGB.
    def known(field, default):
        value = meta.get(field)
        return value if value and value not in ("unknown", "unspecified", "reserved") else default
    src = ":".join([f"primariesin={known('color_primaries', 'bt709')}",
                    f"transferin={known('color_transfer', 'iec61966-2-1' if rgb else 'bt709')}",
                    f"matrixin={known('color_space', 'gbr' if rgb else 'bt709')}",
                    "rangein=" + ("full" if known("color_range", "pc" if rgb else "tv") in ("pc", "full") else "limited")])
    dst = ("primaries=bt2020:transfer=smpte2084:matrix=bt2020nc" if pq
           else "primaries=bt709:transfer=bt709:matrix=bt709")
    # zimg performs the transfer/primaries conversion. HDR to SDR is explicitly tone-mapped.
    if source_hdr and not pq:
        return (f"zscale={src}:transfer=linear:npl=100,format=gbrpf32le,"
                f"zscale=primaries=bt709,tonemap=tonemap=mobius:desat=2,"
                f"zscale={dst}:range=limited:dither=error_diffusion,format={pix_fmt}")
    return f"zscale={src}:{dst}:range=limited:npl=100:dither=error_diffusion,format={pix_fmt}"


def _encode(color):
    if color == "bt709":
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-pix_fmt", "yuv420p",
                "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-threads", "2"]
    if color == "bt2020-pq":
        return ["-c:v", "libx265", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p10le",
                "-tag:v", "hvc1", "-x265-params", "pools=2:frame-threads=1:log-level=error",
                "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc"]
    raise ValueError("Choose bt709 (SDR) or bt2020-pq (10-bit HDR).")


def render_storyboard(guides, output, *, frames=120, fps="24", width=768, height=432, cancel=None):
    """Render a CPU cross-dissolve storyboard with exact colors at each guide before encoding."""
    rate = _positive_rate(fps)
    _dimensions(width, height)
    if type(frames) is not int or frames < 1 or not guides:
        raise ValueError("A storyboard needs at least one frame and guide.")
    images = []
    for guide in sorted(guides, key=lambda g: g["frame"]):
        f = guide["frame"]
        if type(f) is not int or not 0 <= f < frames or (images and f == images[-1][0]):
            raise ValueError("Guide frames must be unique integers inside the delivered cut.")
        with Image.open(guide["path"]) as im:
            fit = ImageOps.contain(ImageOps.exif_transpose(im).convert("RGBA"), (width, height))
            canvas = Image.new("RGB", (width, height))
            canvas.paste(fit, ((width - fit.width)//2, (height - fit.height)//2), fit)
            images.append((f, canvas))
    with _output(output, [g["path"] for g in guides]) as temp, tempfile.TemporaryFile() as errors:
        args = [*_ffmpeg(), "-f", "rawvideo", "-pix_fmt", "rgb24", "-s:v", f"{width}x{height}",
                "-r", str(rate), "-i", "pipe:0", "-vf", "scale=out_color_matrix=bt709:out_range=tv",
                *_encode("bt709"), "-movflags", "+faststart", "-f", "mp4", str(temp)]
        process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                   stderr=errors, creationflags=_HIDDEN)
        idx = 0
        try:
            for f in range(frames):
                if cancel and cancel():
                    raise InterruptedError("Storyboard cancelled.")
                while idx + 1 < len(images) and images[idx+1][0] <= f:
                    idx += 1
                start, image = images[idx]
                if f > start and idx + 1 < len(images):
                    end, following = images[idx+1]
                    image = Image.blend(image, following, (f - start) / (end - start))
                process.stdin.write(image.tobytes())
            process.stdin.close()
            if process.wait():
                raise BrokenPipeError()
        except BrokenPipeError:
            errors.seek(0)
            raise ValueError(errors.read().decode("utf-8", "replace")[-2000:]) from None
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            if not process.stdin.closed:
                process.stdin.close()
    return dict(kind="storyboard", label="CPU storyboard", frames=frames, fps=str(rate),
                width=width, height=height, path=str(Path(output).resolve()))


def export_film(clips, output, *, width=1920, height=1080, fps="24", color="bt709",
                audio_tracks=1, cancel=None, soundtracks=(), framing="fit", captions=(), crop_x=50, crop_y=50):
    _dimensions(width, height)
    rate = _positive_rate(fps)
    encoder = _encode(color)
    if type(audio_tracks) is not int or not 0 <= audio_tracks <= 8 or not clips:
        raise ValueError("Choose at least one clip and zero to eight delivery audio tracks.")
    x, y = _ratio(crop_x), _ratio(crop_y)
    if not (0 <= x <= 100 and 0 <= y <= 100):
        raise ValueError("Crop position must be between 0 and 100 percent.")
    frame_filters = {
        "fit": [f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2:reset_sar=1",
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"],
        "fill": [f"scale={width}:{height}:force_original_aspect_ratio=increase:force_divisible_by=2:reset_sar=1",
                 f"crop={width}:{height}:(iw-ow)*{float(x)}/100:(ih-oh)*{float(y)}/100"],
    }
    if framing not in frame_filters:
        raise ValueError("Choose Fit or Center fill for delivery.")
    graph, inputs, video_labels = [], [], []
    audio_labels = [[] for _ in range(audio_tracks)]
    total, total_frames, total_samples = Fraction(0), 0, 0
    cut_list = []
    for i, clip in enumerate(clips):
        path = Path(clip["path"]).resolve(strict=True)
        meta = probe_media(path)
        duration = _ratio(meta["duration_exact"])
        start = _ratio(clip.get("start", 0))
        end = _ratio(clip["end"]) if clip.get("end") is not None else duration
        if end > duration and end - duration <= Fraction(1, 1_000_000):
            end = duration
        if not 0 <= start < end <= duration:
            raise ValueError(f"Clip {i+1} trim is outside its {float(duration):.6g}-second video.")
        origin = meta["start_pts"] * _ratio(meta["video_time_base"])
        absolute_start, absolute_end = start + origin, end + origin
        total += end - start
        boundary = round(total * rate)
        sample_boundary = round(Fraction(boundary, 1) * 48000 / rate)
        count, samples = boundary - total_frames, sample_boundary - total_samples
        if count < 1:
            raise ValueError(f"Clip {i+1} has no frames at the delivery rate.")
        inputs.extend([*_INPUT, "-threads", "2", "-i", str(path)])
        # Keep the picture displayed across an in-point between VFR timestamps.
        # fps discards negative time before start_time; no seek or average-frame indexing.
        filters = [f"trim=end={_seconds(absolute_end)}", f"setpts=PTS-({_seconds(absolute_start)})/TB",
                   f"fps=fps={rate}:start_time=0:round=up", "tpad=stop_mode=clone:stop=-1",
                   f"trim=end_frame={count}", "setpts=PTS-STARTPTS", _delivery_color(meta, color),
                   *frame_filters[framing], "setsar=1"]
        graph.append(f"[{i}:{meta['video_stream']}]" + ",".join(filters) + f"[v{i}]")
        video_labels.append(f"[v{i}]")
        routing = clip.get("audio", [j if j < len(meta["audio_streams"]) else None for j in range(audio_tracks)])
        if not isinstance(routing, (list, tuple)) or len(routing) != audio_tracks:
            raise ValueError(f"Clip {i+1} needs a route for each delivery audio track.")
        for j, source in enumerate(routing):
            if source is None:
                audio = "anullsrc=r=48000:cl=stereo,"
            else:
                if type(source) is not int or not 0 <= source < len(meta["audio_streams"]):
                    raise ValueError(f"Clip {i+1} has no audio stream {source}.")
                stream = meta["audio_streams"][source]["index"]
                audio = (f"[{i}:{stream}]atrim=start={_seconds(absolute_start)}:end={_seconds(absolute_end)},"
                         f"asetpts=PTS-({_seconds(absolute_start)})/TB,aresample=48000:async=1:first_pts=0,"
                         "aformat=sample_rates=48000:sample_fmts=fltp:channel_layouts=stereo,apad,")
            graph.append(audio + f"atrim=end_sample={samples},asetpts=N/SR/TB[a{i}_{j}]")
            audio_labels[j].append(f"[a{i}_{j}]")
        cut_list.append(dict(first_frame=total_frames, end_frame=boundary, audio=list(routing),
                             source_start=float(start), source_end=float(end)))
        total_frames, total_samples = boundary, sample_boundary
    graph.append("".join(video_labels) + f"concat=n={len(clips)}:v=1:a=0,setpts=N/({rate}*TB)[film]")
    mixes = [[] for _ in audio_labels]
    for index, bed in enumerate(soundtracks):
        source, track = bed["path"], bed["track"]
        if not 0 <= track < audio_tracks:
            raise ValueError(f"Soundtrack {index+1} needs output audio track {track+1}.")
        delay = round(_ratio(bed["offset"]) * 48000)
        gain = _ratio(bed["gain_db"])
        if delay < 0 or not -96 <= gain <= 24:
            raise ValueError("Choose a nonnegative soundtrack offset and gain between -96 and +24 dB.")
        if delay >= total_samples:
            continue
        inputs.extend(["-protocol_whitelist", "file,pipe", "-format_whitelist", AUDIO_FORMATS,
                       "-i", str(Path(source).resolve(strict=True))])
        input_number = len(clips) + sum(map(len, mixes))
        label = f"bed{index}"
        graph.append(f"[{input_number}:a:{bed['stream']}]asetpts=PTS-STARTPTS,aresample=48000,"
                     f"aformat=sample_rates=48000:sample_fmts=fltp:channel_layouts=stereo,"
                     f"volume={float(gain)}dB,adelay={delay}S:all=1,atrim=end_sample={total_samples}[{label}]")
        mixes[track].append(f"[{label}]")
    for j, labels in enumerate(audio_labels):
        base = f"base{j}" if mixes[j] else f"track{j}"
        graph.append("".join(labels) + f"concat=n={len(clips)}:v=0:a=1[{base}]")
        if mixes[j]:
            graph.append(f"[{base}]" + "".join(mixes[j]) +
                         f"amix=inputs={1+len(mixes[j])}:normalize=0:duration=first[track{j}]")
    with _output(output, [c["path"] for c in clips] + [b["path"] for b in soundtracks]) as temp, tempfile.TemporaryDirectory() as folder:
        script = Path(folder) / "filters.txt"
        script.write_text(";\n".join(graph), encoding="utf-8")
        from .delivery import write_captions
        subtitle = Path(folder) / "narration.srt"
        cue_count = write_captions(subtitle, captions, float(total_frames / rate))
        if cue_count:
            inputs.extend(["-f", "srt", "-i", str(subtitle)])
        args = [*_ffmpeg(), "-copyts", *inputs, "-/filter_complex", str(script), "-map", "[film]"]
        if cue_count:
            args.extend(["-map", f"{len(clips)+sum(map(len, mixes))}:s:0", "-c:s", "mov_text",
                         "-metadata:s:s:0", "handler_name=Narration", "-disposition:s:0", "default"])
        for j in range(audio_tracks):
            args.extend(["-map", f"[track{j}]"])
        args.extend([*encoder, "-r", str(rate), "-fps_mode", "cfr", "-color_range", "tv",
                     "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-f", "mp4", str(temp)])
        _run(args, cancel)
    return dict(kind="film", frames=total_frames, fps=str(rate), width=width, height=height,
                duration=float(total_frames/rate), audio_tracks=audio_tracks, color=color,
                cuts=cut_list, framing=framing, crop_position=[float(x),float(y)], captions=cue_count, path=str(Path(output).resolve()))


def prepare_h3_join(entries, schema):
    """Build the lineage-checked H3 graph, including 40 Hz latent/decoded route selection."""
    from vendor.prompt.video_join import build_join_transfer
    return build_join_transfer(entries, schema)


def render_edit_branch(*args, **kwargs):
    from .media_edit import render_edit_branch as render
    return render(*args, **kwargs)
