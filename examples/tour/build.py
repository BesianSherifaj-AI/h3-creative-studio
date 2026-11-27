"""Build the narrated vertical and landscape Tidal Relay tours from native workshop captures."""
import argparse
from functools import lru_cache
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import wave

import av
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps, ImageStat

from tour import ART, AUDIO, CHAPTERS, FPS, MIX_RATE, PLATES, ROOT, SIZES, STEM
from tour import captions, chapter_cues, frame_count, lead_time, read_audio


@lru_cache(maxsize=24)
def font(size, heading=False):
    bundled = ROOT / "h3studio/web/fonts/BarlowSemiCondensed-SemiBold.ttf"
    interface = Path(os.environ.get("WINDIR", ".")) / "Fonts/seguisb.ttf"
    path = bundled if heading or not interface.is_file() else interface
    return ImageFont.truetype(str(path), size)


@lru_cache(maxsize=20)
def photo(path):
    with Image.open(path) as image:
        return image.convert("RGB")


def fit_panel(image, rect, size):
    background = Image.new("RGB", size, "#1a2022")
    crop = ImageOps.contain(image.crop(rect), size, method=Image.Resampling.LANCZOS)
    background.paste(crop, ((size[0] - crop.width) // 2, (size[1] - crop.height) // 2))
    return background


def wrap(text, face, width):
    lines = []
    for word in text.split():
        if not lines or face.getlength(lines[-1] + " " + word) > width:
            lines.append(word)
        else:
            lines[-1] += " " + word
    return lines


def base_image(chapter, hero, detail, label, vertical):
    light = chapter in ("image", "film", "end")
    paper, ink, muted, rule = (("#e6e5da", "#162722", "#486356", "#b3bdb2") if light else
                               ("#101918", "#f1f3e9", "#b6c9bf", "#344941"))
    image = Image.new("RGB", SIZES["vertical" if vertical else "landscape"], paper)
    draw = ImageDraw.Draw(image)
    room, heading = ART[chapter][:2]

    def text(x, y, value, size, color=ink, *, bold=False, anchor="lt"):
        draw.text((x, y), value, font=font(size, bold), fill=color, anchor=anchor)

    x, y, side = (64, 95, 52) if vertical else (78, 67, 51)
    draw.rounded_rectangle((x, y, x+side, y+side), radius=10, fill="#aedbc5")
    text(x + side / 2, y + side / 2 - 1, "H3", 31, "#142d23", bold=True, anchor="mm")
    text(x + side + 14, y + side / 2, "Creative Studio", 30, anchor="lm")
    if vertical:
        draw.line((64, 178, 1016, 178), fill=rule, width=2)
        text(65, 219, room, 28, muted)
        text(60, 273, heading, 92, bold=True)
        image.paste(hero, (64, 425))
        draw.line((64, 960, 1015, 960), fill=rule, width=2)
        text(65, 1006, label, 35)
        image.paste(detail, (64, 1072))
        draw.line((64, 1450, 1016, 1450), fill=rule, width=2)
    else:
        draw.line((78, 142, 1842, 142), fill=rule, width=2)
        text(80, 181, room, 25, muted)
        text(76, 222, heading, 83, bold=True)
        image.paste(hero, (78, 342))
        image.paste(detail, (1184, 398))
        text(1185, 344, label, 31)
        draw.line((78, 908, 1842, 908), fill=rule, width=2)
    if chapter == "end":
        x, y, px = (65, 1412, 32) if vertical else (1185, 803, 27)
        text(x, y, "github.com/BesianSherifaj-AI/h3-creative-studio", px, muted)
    return image, ink


def capture_inputs(captures):
    receipt = json.loads((captures / "verified.json").read_text("utf-8-sig"))
    if not (receipt.get("frames") == 240 and receipt.get("assistant_subprocess_reviewed")
            and receipt.get("narration_audition") and receipt.get("game_board")):
        raise ValueError("Choose a workshop run that completed the native Game, Voice, Film and AI client checks.")
    bounds = json.loads((captures / "tour-bounds.json").read_text("utf-8-sig"))
    named = {}
    images = set()
    for _, _, primary, _, details in ART.values():
        if not primary.startswith("@"):
            images.add(captures / (primary + ".jpg"))
        for shot, rect, _ in details:
            images.add(captures / (shot + ".jpg"))
            if isinstance(rect, str):
                values = bounds.get(shot, {}).get(rect)
                if not isinstance(values, list) or len(values) != 4:
                    raise ValueError(f"Missing {shot}.{rect} in tour-bounds.json.")
                left, top, right, bottom = values
                if not (0 <= left < right <= 1440 and 0 <= top < bottom <= 900):
                    raise ValueError(f"Invalid capture rectangle for {shot}.{rect}: {values}")
                named[shot, rect] = tuple(round(v) for v in values)
    still_region = (380, 360, 995, 675)
    before, after = (photo(captures / name) for name in ("image-title.jpg", "image-edited.jpg"))
    if min(max(ImageStat.Stat(im.crop(still_region)).stddev) for im in (before, after)) < 10:
        raise ValueError("The layered composition is blank in a workshop capture.")
    crop = ART["image"][3]
    if max(ImageStat.Stat(ImageChops.difference(before.crop(crop), after.crop(crop))).mean) < 1:
        raise ValueError("The title layer did not visibly move between captures.")
    for shot, monitor in (("game-shore", (165, 275, 800, 568)), ("edit", (215, 180, 840, 420))):
        stream = [captures / ("record-" + shot) / f"{index:03}.jpg" for index in range(17)]
        for frame in stream:
            with Image.open(frame) as capture:
                if max(ImageStat.Stat(capture.crop(monitor)).stddev) < 10:
                    raise ValueError(f"The {shot} monitor is blank in recorded frame {frame.name}.")
        images.update(stream)
    for path in images:
        with Image.open(path) as image:
            if image.size != (1440, 900):
                raise ValueError(f"{path.name} must be a 1440 x 900 native screenshot.")
    return named


def original_take(name):
    """Loop the bundled camera move at its authored 24 fps."""
    while True:
        with av.open(str(PLATES / (name + ".mp4"))) as container:
            video = container.streams.video[0]
            if video.average_rate != FPS:
                raise ValueError(f"Expected an original 24 fps camera move in {name}.mp4.")
            yielded = False
            for frame in container.decode(video=0):
                yielded = True
                yield frame.to_image()
            if not yielded:
                raise ValueError(f"Empty video source {name}.mp4")


def delivery_pair(captures, size):
    """Show the two real Studio exports from the same frame of the film."""
    with av.open(str(captures / "Narrated vertical.mp4")) as center, av.open(str(captures / "Reframed vertical.mp4")) as shifted:
        for index, (a, b) in enumerate(zip(center.decode(video=0), shifted.decode(video=0))):
            if a.pts * a.time_base != b.pts * b.time_base:
                raise ValueError("The exported crops are out of sync.")
            left, right = a.to_image(), b.to_image()
            if index == 0 and max(ImageStat.Stat(ImageChops.difference(left, right)).mean) < 8:
                raise ValueError("The exported before and after crops show the same picture.")
            pair = Image.new("RGB", size, "#1a2423")
            draw = ImageDraw.Draw(pair)
            for column, (picture, label) in enumerate(((left, "Center crop"), (right, "Reframed"))):
                image = ImageOps.contain(picture, (size[0]//2, size[1]-57), Image.Resampling.LANCZOS)
                x = (size[0]//2 - image.width)//2 + column * (size[0]//2)
                pair.paste(image, (x, 0))
                draw.text((x, size[1]-40), label, font=font(28, True), fill="#f1f3e9", anchor="lt")
            yield pair


def render_chapter(writer, captures, named, chapter, aspect):
    key = chapter["id"]
    _, _, primary, region, details = ART[key]
    vertical = aspect == "vertical"
    hero_size = (952, 535) if vertical else (1068, 539)
    panel_size = (952, 332) if vertical else (658, 360)
    panels = []
    for shot, box, label in details:
        rect = named[shot, box] if isinstance(box, str) else box
        panels.append((fit_panel(photo(captures / (shot + ".jpg")), rect, panel_size), label))
    reel = original_take(primary[1:]) if primary.startswith("@") else None
    recorded = sorted((captures / ("record-" + primary)).glob("*.jpg")) if not reel else []
    crops = delivery_pair(captures, hero_size) if key == 'delivery' else None
    length = frame_count(chapter)
    cues = list(chapter_cues(chapter))
    lead = float(lead_time(chapter))
    previous = None
    try:
        for frame in range(length):
            portion = frame / max(1, length - 1)
            step = min(len(details) - 1, int(portion * len(details)))
            still = primary
            if key == "image" and portion > .5:
                still = "image-edited"
            if key == "game" and portion > .68:
                still = "game-ending"
            comparing = crops is not None and step >= 1
            if reel or comparing:
                source = frame
            elif recorded and still == primary:
                source = recorded[min(len(recorded) - 1, frame * 8 // FPS)]
            else:
                source = captures / (still + ".jpg")
            cache_key = source, step
            if cache_key != previous:
                if comparing:
                    hero = next(crops)
                else:
                    picture = next(reel) if reel else photo(source)
                    hero = ImageOps.fit(picture.crop(region) if region else picture, hero_size,
                                        method=Image.Resampling.LANCZOS)
                base, ink = base_image(key, hero, *panels[step], vertical)
                previous = cache_key
            image = base.copy()
            spoken = frame / FPS - lead
            caption = next((text for text, a, b in cues if a - .03 <= spoken < b + .02), "")
            if caption:
                x, y, px, leading, width = (65, 1508, 48, 61, 872) if vertical else (78, 945, 39, 48, 1680)
                draw = ImageDraw.Draw(image)
                for n, line in enumerate(wrap(caption, font(px), width)):
                    draw.text((x, y + n*leading), line, font=font(px), fill=ink, anchor="lt")
            writer.write(image.tobytes())
    finally:
        if reel:
            reel.close()
        if crops:
            crops.close()


def make_voice(ffmpeg, path, audio, chapters):
    with wave.open(str(path), "wb") as mix:
        mix.setparams((1, 2, MIX_RATE, 0, "NONE", "not compressed"))
        for chapter in chapters:
            source = audio / (chapter["id"] + ".wav")
            pcm = subprocess.check_output([ffmpeg, "-v", "error", "-nostdin", "-i", str(source),
                                           "-ar", str(MIX_RATE), "-ac", "1", "-f", "s16le", "-"])
            lead_bytes = int(lead_time(chapter) * MIX_RATE) * 2
            total_bytes = frame_count(chapter) * (MIX_RATE // FPS) * 2
            mix.writeframes(bytes(lead_bytes) + pcm + bytes(total_bytes - lead_bytes - len(pcm)))


def build(aspect, tmp, output, captures, named, chapters, ffmpeg, voice):
    width, height = SIZES[aspect]
    silent = tmp / (aspect + "-picture.mp4")
    args = [ffmpeg, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-video_size",
            f"{width}x{height}", "-framerate", str(FPS), "-i", "-", "-vf",
            "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "18", "-threads", "5", "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace",
            "bt709", str(silent)]
    with subprocess.Popen(args, stdin=subprocess.PIPE) as encoder:
        try:
            for chapter in chapters:
                print(aspect, chapter["id"], frame_count(chapter), "frames", flush=True)
                render_chapter(encoder.stdin, captures, named, chapter, aspect)
        finally:
            encoder.stdin.close()
        if encoder.wait():
            raise RuntimeError("FFmpeg did not finish the picture edit.")
    picture_frames = sum(frame_count(chapter) for chapter in chapters)
    duration = picture_frames / FPS
    samples = picture_frames * (MIX_RATE // FPS)
    mix = (f"[1:a]highpass=f=65,loudnorm=I=-16:LRA=7:TP=-2,aresample=48000,pan=stereo|c0=c0|c1=c0[v];"
           f"[2:a]atrim=duration={duration},asetpts=PTS-STARTPTS,loudnorm=I=-32:LRA=9:TP=-6,aresample=48000,"
           f"afade=t=out:st={duration-1.2}:d=1.2[m];[v][m]amix=inputs=2:normalize=0:duration=first,"
           "alimiter=limit=0.9:level=0:latency=1,aresample=48000,"
           f"apad=whole_len={samples},atrim=end_sample={samples},asetpts=N/SR/TB[mix]")
    movie = tmp / f"{STEM}-{aspect}.mp4"
    subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-y", "-i", str(silent), "-i", str(voice),
                    "-stream_loop", "-1", "-i", str(PLATES / "listening-line.m4a"), "-filter_complex", mix,
                    "-map", "0:v", "-map", "[mix]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                    "-ar", str(MIX_RATE), "-movie_timescale", str(MIX_RATE), "-movflags", "+faststart", "-metadata", "title=H3 Creative Studio | Tidal Relay",
                    "-metadata", "comment=Native Studio UI, original Tidal Relay score and synthesized narration.",
                    str(movie)], check=True)
    movie.replace(output / movie.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--captures", type=Path, required=True, help="Completed 1440 x 900 --record workshop run")
    parser.add_argument("--output", type=Path, required=True, help="Directory for the MP4 masters and English SRT")
    parser.add_argument("--audio", type=Path, default=AUDIO, help="Folder with narration.json and its nine WAV masters")
    parser.add_argument("--format", choices=["both", *SIZES], default="both")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="FFmpeg executable name or path")
    parser.add_argument("--check-inputs", action="store_true", help="Validate the captures and narration, then exit")
    args = parser.parse_args()
    chapters = read_audio(args.audio)
    named = capture_inputs(args.captures)
    if args.check_inputs:
        print(f"Validated {len(chapters)} chapters and {sum(map(frame_count, chapters))} delivery frames.")
        return
    ffmpeg = shutil.which(args.ffmpeg)
    if not ffmpeg:
        parser.error("FFmpeg could not be found. Use --ffmpeg to select its executable.")
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.output, prefix="tour-work-") as work:
        tmp = Path(work)
        voice = tmp / "narration.wav"
        make_voice(ffmpeg, voice, args.audio, chapters)
        for aspect in (SIZES if args.format == "both" else [args.format]):
            build(aspect, tmp, args.output, args.captures, named, chapters, ffmpeg, voice)
        srt = tmp / (STEM + ".en.srt")
        srt.write_text(captions(chapters), encoding="utf-8")
        srt.replace(args.output / srt.name)


if __name__ == "__main__":
    main()
