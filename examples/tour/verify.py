"""Decode both tour masters, check the captions and audio, and write contact sheets and a receipt."""
import argparse
from bisect import bisect_right
from itertools import accumulate
from fractions import Fraction
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import av
from PIL import Image, ImageDraw, ImageStat

from tour import AUDIO, FPS, MIX_RATE, SIZES, STEM, captions, frame_count, read_audio, sha256


def require(condition, message):
    if not condition:
        raise ValueError(message)


def seconds(timecode):
    hours, minutes, secs, millis = map(int, re.split("[:,]", timecode))
    return hours*3600 + minutes*60 + secs + millis/1000


def verify_captions(folder, chapters, duration):
    path = folder / (STEM + ".en.srt")
    text = path.read_text("utf-8-sig").strip()
    require(text == captions(chapters).strip(), "The sidecar differs from the aligned narration.")
    previous = 0
    blocks = text.split("\n\n")
    for number, block in enumerate(blocks, 1):
        idx, span, line = block.split("\n", 2)
        start, end = map(seconds, span.split(" --> "))
        require(int(idx) == number and previous <= start < end <= duration and line.strip(), f"Invalid cue {number}")
        previous = end
    return len(blocks)


def loudness(ffmpeg, path):
    result = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-i", str(path),
                             "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"],
                            capture_output=True, text=True, errors="replace", check=True)
    summary = result.stderr.split("Summary:")[-1]
    loud = re.search(r"I:\s+(-?[\d.]+) LUFS", summary)
    peak = re.search(r"Peak:\s+(-?[\d.]+) dBFS", summary)
    require(loud and peak, "FFmpeg did not report integrated loudness and true peak.")
    lufs, true_peak = float(loud[1]), float(peak[1])
    require(-21 <= lufs <= -12 and true_peak <= -.2,
            f"{path.name} measured {lufs} LUFS and {true_peak} dBTP.")
    return lufs, true_peak


def inspect_movie(path, aspect, work, chapters, expected, ffmpeg):
    size = SIZES[aspect]
    count = sum(expected)
    cell_height = 160 if aspect == "landscape" else 276
    sheet = Image.new("RGB", (900, 3*(cell_height + 30)), "#121c1d")
    moments = {sum(expected[:i]) + int(length*.43): i for i, length in enumerate(expected)}
    visited = set()
    frames = 0
    edit_chapter = next(i for i, chapter in enumerate(chapters) if chapter["id"] == "delivery")
    edit_start = sum(expected[:edit_chapter])
    edit_end = edit_start + expected[edit_chapter]
    # Both probe points stay inside live picture before and after the two-up crop comparison.
    left, right = ((.30,.70) if aspect == "landscape" else (.28,.72))
    if aspect == "landscape":
        left, right = (78 + 1068*left) / size[0], (78 + 1068*right) / size[0]
        top, bottom = (342 + 539*.18) / size[1], (342 + 539*.72) / size[1]
    else:
        left, right = (64 + 952*left) / size[0], (64 + 952*right) / size[0]
        top, bottom = (425 + 535*.18) / size[1], (425 + 535*.72) / size[1]
    monitor_min = float("inf")
    chapter_ends = list(accumulate(expected))
    picture_min = {ch["id"]: float("inf") for ch in chapters}
    with av.open(str(path)) as source:
        require(len(source.streams.video) == 1 and len(source.streams.audio) == 1, "Expected a picture and a stereo mix.")
        video, audio = source.streams.video[0], source.streams.audio[0]
        require((video.width,video.height) == size and video.average_rate == Fraction(FPS, 1), f"Invalid format in {path.name}")
        require(video.codec_context.name == "h264" and video.codec_context.format.name == "yuv420p", "Expected H.264 4:2:0.")
        require(audio.codec_context.name == "aac" and audio.rate == MIX_RATE and audio.layout.name == "stereo", "Expected stereo AAC at 48 kHz.")
        for frame in source.decode(video=0):
            require(frame.pts is not None and abs(float(frame.pts*frame.time_base) - frames/FPS) < .0005,
                    f"Unexpected presentation time at frame {frames} in {path.name}")
            probe = frame.reformat(width=192, height=192, format="gray").to_ndarray()
            chapter_id = chapters[bisect_right(chapter_ends, min(frames,count-1))]["id"]
            rows = probe[round(top*192):round(bottom*192)]
            variation = float(rows[:,round(left*192):round(right*192)].std())
            require(variation > 7, f"Blank {chapter_id} picture in {path.name} at frame {frames}")
            picture_min[chapter_id] = min(picture_min[chapter_id], variation)
            if edit_start <= frames < edit_end:
                for x in (left, right):
                    columns = slice(round((x-.025)*192), round((x+.025)*192))
                    detail = float(probe[round(top*192):round(bottom*192), columns].std())
                    require(detail > 5, f"Blank Edit picture in {path.name} at frame {frames}")
                    monitor_min = min(monitor_min, detail)
            chapter = moments.get(frames)
            if chapter is not None:
                visited.add(chapter)
                image = frame.to_image()
                image.thumbnail((288, cell_height), Image.Resampling.LANCZOS)
                require(max(ImageStat.Stat(image).stddev) > 8, f"Flat review frame in {chapters[chapter]['id']}")
                x = 300*(chapter % 3) + (300-image.width)//2
                y = (chapter//3)*(cell_height+30)
                sheet.paste(image,(x,y))
                ImageDraw.Draw(sheet).text((x,y+image.height+4), chapters[chapter]["id"])
            frames += 1
    require(frames == count and len(visited) == len(chapters), f"Truncated picture in {path.name}")
    with av.open(str(path)) as source:
        audio = source.streams.audio[0]
        expected_samples = frames * (MIX_RATE // FPS)
        declared_samples = round(audio.duration * audio.time_base * MIX_RATE)
        samples = 0
        for frame in source.decode(audio=0):
            require(frame.pts is not None and abs(frame.pts * frame.time_base * MIX_RATE - samples) <= 1,
                    f"Audio discontinuity at sample {samples} in {path.name}")
            samples += frame.samples
    with av.open(str(path)) as source:
        audio = source.streams.audio[0]
        priming = discard = 0
        first_pts = last_end = None
        for packet in source.demux(audio):
            if packet.pts is None:
                continue
            if first_pts is None:
                first_pts = packet.pts
            last_end = packet.pts + packet.duration
            for side in packet.iter_sidedata():
                if side.data_type == "skip_samples":
                    data = bytes(side)
                    priming += int.from_bytes(data[:4], "little")
                    discard += int.from_bytes(data[4:8], "little")
        timing = dict(expected_samples=expected_samples, presented_samples=declared_samples, decoded_samples=samples,
                      stream_start_pts=audio.start_time, stream_duration=audio.duration, time_base=str(audio.time_base),
                      first_packet_pts=first_pts, last_packet_end_pts=last_end, skip_samples=priming,
                      discard_samples=discard, decoded_tail_padding=samples-declared_samples)
    print(path.name, json.dumps(timing), flush=True)
    require(abs(declared_samples - expected_samples) <= 1 and abs(samples - expected_samples) < 1024,
            f"Audio length mismatch in {path.name}: expected {expected_samples}, "
            f"stream declares {declared_samples}, decoded {samples}.")
    lufs, peak = loudness(ffmpeg, path)
    sheet.save(work / f"review-{aspect}.jpg", quality=92)
    return {"size": size, "frames":frames, "seconds":frames/FPS, "audio_samples":samples, "declared_audio_samples":declared_samples, "audio_timing":timing,
            "lufs":lufs, "true_peak":peak, "chapter_picture_min_stddev":{k:round(v,2) for k,v in picture_min.items()}, "edit_picture_check":{"first":edit_start,"last":edit_end-1,"minimum_luma_stddev":round(monitor_min,2)}, "sha256":sha256(path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Folder containing both MP4 masters and English SRT")
    parser.add_argument("--audio", type=Path, default=AUDIO, help="Directory containing narration.json and the WAVs used for the edit")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="FFmpeg executable with the ebur128 filter")
    args = parser.parse_args()
    chapters = read_audio(args.audio)
    expected = [frame_count(c) for c in chapters]
    ffmpeg = shutil.which(args.ffmpeg)
    if not ffmpeg:
        parser.error("FFmpeg could not be found. Use --ffmpeg to select its executable.")
    cues = verify_captions(args.output, chapters, sum(expected)/FPS)
    with tempfile.TemporaryDirectory(dir=args.output) as folder:
        work = Path(folder)
        receipt = {"caption_cues": cues}
        failures = {}
        for aspect in SIZES:
            path = args.output / f"{STEM}-{aspect}.mp4"
            try:
                receipt[aspect] = inspect_movie(path, aspect, work, chapters, expected, ffmpeg)
            except (OSError, ValueError, subprocess.CalledProcessError) as error:
                failures[aspect] = str(error)
        if failures:
            raise ValueError(json.dumps(failures))
        report = work / "tour-verified.json"
        report.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        for file in work.iterdir():
            file.replace(args.output / file.name)
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
