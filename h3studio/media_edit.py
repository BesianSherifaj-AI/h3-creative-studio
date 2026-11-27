"""Lossless RGB editing branches keyed to the source video's display timestamps."""
from __future__ import annotations

from fractions import Fraction
from pathlib import Path
import hashlib
import json
import os
import shutil
import tempfile

from .media import _INPUT, _ffmpeg, _run, frame_index, probe_media


def _read_image(path, dimensions, mask=False):
    import cv2
    import numpy as np
    image = cv2.imdecode(np.fromfile(path, dtype=np.uint8),
                         cv2.IMREAD_GRAYSCALE if mask else cv2.IMREAD_UNCHANGED)
    if image is None or image.shape[:2][::-1] != dimensions:
        raise ValueError("A source edit or mask has different display dimensions.")
    if mask:
        return image
    if image.ndim == 2:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
    else:
        image = cv2.cvtColor(image, cv2.COLOR_BGRA2RGB if image.shape[2] == 4 else cv2.COLOR_BGR2RGB)
    return image if image.dtype == np.uint16 else image.astype(np.uint16) * 257


def _track(previous, current, mask):
    """Propagate a translation on a bounded working copy, retaining the full-resolution mask."""
    import cv2
    import numpy as np
    height, width = mask.shape
    scale = min(1., 960 / max(width, height))
    size = (max(1, round(width*scale)), max(1, round(height*scale)))
    m = cv2.resize(mask, size, interpolation=cv2.INTER_NEAREST)
    x, y, w, h = cv2.boundingRect(m)
    if not w or not h:
        raise ValueError("The current selection is empty. Mark this frame Hidden or provide a keyframe.")
    def view(frame):
        return cv2.cvtColor(cv2.resize((frame >> 8).astype(np.uint8), size, interpolation=cv2.INTER_AREA),
                            cv2.COLOR_RGB2GRAY)
    # Margin preserves object edges even for a nearly uniform selection.
    left, top = max(0, x-3), max(0, y-3)
    right, bottom = min(size[0], x+w+3), min(size[1], y+h+3)
    template = view(previous)[top:bottom, left:right]
    radius = max(7, min(48, max(w, h)//2))
    sl, st = max(0, left-radius), max(0, top-radius)
    search = view(current)[st:min(size[1], bottom+radius), sl:min(size[0], right+radius)]
    scores = cv2.matchTemplate(search, template, cv2.TM_SQDIFF)
    # Treat separated, equally plausible locations as ambiguous.
    _, _, best, _ = cv2.minMaxLoc(scores)
    err = float(max(0, scores[best[1], best[0]]) / template.size)
    delta_x, delta_y = sl+best[0]-left, st+best[1]-top
    comparable = scores.copy()
    gap = max(2, min(6, min(w, h)//3))
    comparable[max(0,best[1]-gap):best[1]+gap+1, max(0,best[0]-gap):best[0]+gap+1] = float("inf")
    second_err = float(comparable.min() / template.size)
    if err > 35**2 or second_err < err + 12:
        raise ValueError("The moving selection is ambiguous. Add a correction keyframe or mark it Hidden.")
    matrix = np.float32([[1,0,round(delta_x/scale)], [0,1,round(delta_y/scale)]])
    return cv2.warpAffine(mask, matrix, (width, height), flags=cv2.INTER_NEAREST)


def _paint(frame, mask, action, rgb, replacement=None):
    import cv2
    import numpy as np
    x, y, w, h = cv2.boundingRect(mask)
    if not w or not h:
        return frame
    # Keep effects local and preserve every unselected 16-bit RGB pixel.
    bx, by = max(0, x-16), max(0, y-16)
    box = np.s_[by:min(frame.shape[0], y+h+16), bx:min(frame.shape[1], x+w+16)]
    original = frame[box]
    soft = mask[box]
    if replacement is not None:
        edited = replacement[box]
    elif action == "blur":
        edited = cv2.GaussianBlur(original, (0,0), 5)
    elif action == "remove":
        hole = (soft > 0).astype(np.uint8) * 255
        edited = np.stack([cv2.inpaint(original[:,:,channel], hole, 3, cv2.INPAINT_TELEA)
                           for channel in range(3)], axis=2)
    else:
        luminance = cv2.cvtColor(original, cv2.COLOR_RGB2GRAY).astype(np.float32)
        tint = np.asarray(rgb, dtype=np.float32)
        edited = np.clip(luminance[:,:,None] * tint / max(1., float(tint @ [.2126,.7152,.0722])),
                         0, 65535).astype(np.uint16)
    output = frame.copy()
    weight = soft.astype(np.uint32)[:,:,None]
    output[box] = ((original.astype(np.uint32)*(255-weight) +
                    edited.astype(np.uint32)*weight + 127)//255).astype(np.uint16)
    return output


def render_edit_branch(path, output_dir, anchors, *, start_pts, end_pts, action="recolor", color="#657dff", cancel=None):
    """Render an inclusive source-PTS range. Preserve the full cut, audio tracks, masks and corrections.

    Start/end and every anchor must be actual frame PTS. For a single frame use equal endpoints.
    An anchor image supplies an exact one-frame edit. Subsequent frames use the chosen operation
    inside the propagated selection, unless an explicit replacement is supplied there too.
    """
    import av
    import cv2
    import numpy as np
    from PIL import ImageColor

    source = Path(path).resolve(strict=True)
    destination = Path(output_dir).resolve()
    if destination.exists():
        raise ValueError("Use a fresh edit branch folder to keep earlier versions.")
    if action not in ("blur", "recolor", "remove"):
        raise ValueError("Choose a blur, recolor or remove operation.")
    rgb = ImageColor.getrgb(color)
    if len(rgb) != 3:
        raise ValueError("Choose an RGB edit color.")
    meta = probe_media(source)
    frames = frame_index(source)
    known = {f["pts"] for f in frames}
    if start_pts not in known or end_pts not in known or start_pts > end_pts:
        raise ValueError("The edit endpoints must be actual source frame timestamps in order.")
    keyed = {}
    for a in anchors:
        p, state = a["pts"], a.get("state", "keyframe")
        if p not in known or not start_pts <= p <= end_pts or p in keyed:
            raise ValueError("Correction keys must be unique source timestamps inside the edit range.")
        if state not in ("hidden", "keyframe", "reappears"):
            raise ValueError("Use keyframe, hidden or reappears.")
        if state != "hidden" and not a.get("mask") and not a.get("image"):
            raise ValueError("A visible correction needs a source-sized mask or edited frame.")
        keyed[p] = a
    if start_pts not in keyed:
        raise ValueError("Mark the first frame of the range with a keyframe or Hidden.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    working = Path(tempfile.mkdtemp(prefix=".h3-edit-", dir=destination.parent))
    try:
        (working/"masks").mkdir()
        mask = previous = None
        corrected = []
        dims = (meta["width"], meta["height"])
        base = Fraction(meta["video_time_base"])
        with av.open(str(source), options={"protocol_whitelist":"file,pipe","format_whitelist":_INPUT[-1]}) as inp, \
             av.open(str(working/"working.nut"), "w", format="nut") as out:
            stream = inp.streams[meta["video_stream"]]
            stream.thread_type = "SLICE"
            stream.codec_context.thread_count = 2
            video = out.add_stream("ffv1", rate=stream.average_rate or Fraction(24))
            video.width, video.height = dims
            video.pix_fmt = "gbrp16le"
            video.time_base = video.codec_context.time_base = base
            video.codec_context.thread_count = 2
            video.codec_context.color_range = 2  # full-range RGB
            video.codec_context.colorspace = 0    # RGB/GBR
            video.codec_context.color_trc = stream.codec_context.color_trc
            video.codec_context.color_primaries = stream.codec_context.color_primaries
            decoded = 0
            for index, original_frame in enumerate(inp.decode(stream)):
                if cancel and cancel():
                    raise InterruptedError("Edit cancelled.")
                p = original_frame.pts
                if index >= len(frames) or p != frames[index]["pts"]:
                    raise ValueError("The source decoder disagrees with the indexed presentation timestamps.")
                raw = original_frame.to_ndarray(format="rgb48le")
                frame = np.ascontiguousarray(np.rot90(raw, k=meta["rotation"]//90))
                edited = frame
                if start_pts <= p <= end_pts:
                    anchor = keyed.get(p)
                    state = anchor.get("state", "keyframe") if anchor else ("tracked" if mask is not None else "hidden")
                    image = None
                    if anchor:
                        if state == "hidden":
                            mask = None
                        else:
                            mask = (_read_image(anchor["mask"], dims, True) if anchor.get("mask") else
                                    np.full(frame.shape[:2], 255, dtype=np.uint8))
                            if not mask.any():
                                raise ValueError("A visible correction cannot use an empty mask.")
                            if anchor.get("image"):
                                image = _read_image(anchor["image"], dims)
                    elif mask is not None:
                        try:
                            mask = _track(previous, frame, mask)
                        except ValueError as exc:
                            raise ValueError(f"Source PTS {p}: {exc}") from exc
                    record = dict(pts=p, index=index, state=state)
                    if mask is not None:
                        rel = f"masks/{index:09d}.png"
                        ok, png = cv2.imencode(".png", mask)
                        if not ok:
                            raise ValueError("Mask encoding failed.")
                        (working/rel).write_bytes(png.tobytes())
                        record["mask"] = rel
                        edited = _paint(frame, mask, action, rgb, image)
                        if image is not None:
                            target = working/f"replacement-{index:09d}.png"
                            ok, png = cv2.imencode(".png", cv2.cvtColor(image,cv2.COLOR_RGB2BGR))
                            if not ok:
                                raise ValueError("Correction encoding failed.")
                            target.write_bytes(png.tobytes())
                            record["image"] = target.name
                    corrected.append(record)
                    previous = frame
                output_frame = av.VideoFrame.from_ndarray(edited, format="rgb48le")
                output_frame.pts, output_frame.time_base = p, base
                for packet in video.encode(output_frame):
                    out.mux(packet)
                decoded += 1
            if decoded != len(frames):
                raise ValueError("Source frame count changed during the edit.")
            for packet in video.encode():
                out.mux(packet)
        # Decode primed audio (e.g. negative AAC packets) to lossless PCM. NUT preserves VFR.
        audio_codecs = []
        for index, track in enumerate(meta["audio_streams"]):
            codec = "copy" if str(track["codec"]).startswith("pcm_") else "pcm_f64le"
            audio_codecs.extend([f"-c:a:{index}", codec])
        transfer = meta["color_transfer"]
        if transfer == "unknown":
            transfer = "iec61966-2-1" if (meta["pix_fmt"] or "").startswith(("rgb","gbr","bgr")) else "bt709"
        primaries = "bt709" if meta["color_primaries"] == "unknown" else meta["color_primaries"]
        _run([*_ffmpeg(), "-copyts", "-i", str(working/"working.nut"), *_INPUT, "-i", str(source),
              "-map", "0:v:0", "-map", "1:a?", "-c:v", "copy", *audio_codecs, "-avoid_negative_ts", "make_non_negative",
              "-metadata:s:v:0", "h3_transfer="+transfer, "-metadata:s:v:0", "h3_primaries="+primaries,
              "-f", "nut", str(working/"edited.nut")], cancel)
        (working/"working.nut").unlink()
        with source.open("rb") as data:
            digest = hashlib.file_digest(data, "sha256").hexdigest()
        record = dict(version=1, source_sha256=digest, source_time_base=meta["video_time_base"],
                      source_rotation=meta["rotation"], start_pts=start_pts, end_pts=end_pts,
                      action=action, color=color, corrections=corrected, video="edited.nut")
        (working/"branch.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        os.rename(working, destination)
        return {**record, "path":str(destination/"edited.nut"), "manifest":str(destination/"branch.json")}
    finally:
        if working.is_dir():
            shutil.rmtree(working)
