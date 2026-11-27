"""Shared edit timing, narration and chapter art direction for the release films."""
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import wave

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
AUDIO = HERE / "audio"
PLATES = ROOT / "h3studio/starter"
FPS = 24
MIX_RATE = 48000
SIZES = {"vertical": (1080, 1920), "landscape": (1920, 1080)}
STEM = "H3-Studio-Tour"
CHAPTERS = ("opening", "game", "image", "motion", "film", "voice", "delivery", "assistant", "end")

# Each detail names a native screenshot and its capture-pixel rectangle. Named
# rectangles come from the capture's DOM geometry and follow the export dialog.
ART = {
    "opening": ("TIDAL RELAY", "A signal in the fog.", "@headland", None, [
        ("game-opening", (931, 226, 1378, 435), "Your next move")]),
    "game": ("GAME", "Take the lamp.", "game-shore", (65, 224, 902, 632), [
        ("game-shore", (930, 230, 1380, 397), "Choose a route"),
        ("game-ending", (929, 230, 1380, 476), "Reach an ending"),
        ("branch-board", (20, 291, 620, 530), "Move the route into Film")]),
    "image": ("IMAGE", "Move one layer.", "image-title", (251, 275, 1136, 779), [
        ("image-title", (1150, 201, 1430, 423), "Select the title layer"),
        ("image-edited", (1150, 427, 1425, 570), "Set its position")]),
    "motion": ("MOTION", "Guide the camera.", "@receiver", None, [
        ("motion", (1154, 319, 1435, 432), "First, middle and last"),
        ("guide-middle", (475, 232, 967, 444), "Inspect the saved revision"),
        ("guide-last", (475, 232, 967, 444), "Check the last frame")]),
    "film": ("FILM", "Keep the take.", "film-lock", (320, 316, 1080, 709), [
        ("film-lock", (27, 304, 305, 530), "Lock a selected take"),
        ("film-lock", (322, 712, 1091, 837), "Open it on the timeline")]),
    "voice": ("NARRATION", "Voice the keeper.", "@breakwater", None, [
        ("voice-script", (475, 214, 960, 421), "Write a character line"),
        ("voice-listen", (476, 259, 962, 595), "Audition the voice"),
        ("voice-mix", (3, 538, 885, 807), "Place it under the cut")]),
    "delivery": ("EDIT", "Reframe the cut.", "edit", (0, 95, 1117, 536), [
        ("voice-mix", (3, 548, 888, 807), "Mix dialogue and score"),
        ("portrait", "preset", "Choose a vertical delivery"),
        ("portrait-crop", "crop", "Adjust the crop position"),
        ("portrait", "captions", "Include timed captions")]),
    "assistant": ("AI CLIENTS", "Review the change.", "@receiver", None, [
        ("assistant-pair", (471, 224, 965, 502), "Choose the client's permission"),
        ("assistant-inbox", (473, 439, 961, 669), "Review a proposed shot"),
        ("assistant-applied", (1154, 416, 1433, 536), "Use the approved prompt")]),
    "end": ("H3 CREATIVE STUDIO", "Play Tidal Relay.", "@breakwater", None, [
        ("examples", (275, 148, 1162, 462), "Eight editable examples")]),
}


def sha256(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def read_audio(folder):
    chapters = json.loads((folder / "narration.json").read_text("utf-8"))["chapters"]
    if tuple(c["id"] for c in chapters) != CHAPTERS:
        raise ValueError("The narration manifest must include the nine tour chapters in edit order.")
    for chapter in chapters:
        path = folder / (chapter["id"] + ".wav")
        if sha256(path) != chapter["sha256"]:
            raise ValueError(f"Narration digest mismatch for {path.name}")
        with wave.open(str(path)) as source:
            if (source.getsampwidth(), source.getnchannels(), source.getframerate(), source.getnframes()) != (
                    2, 1, chapter["sample_rate"], chapter["frames"]):
                raise ValueError(f"Unexpected WAV format or length in {path.name}")
        words = chapter["words"]
        if " ".join(w["text"] for w in words) != chapter["text"]:
            raise ValueError(f"Alignment differs from the spoken script in {path.name}")
        previous = 0
        for word in words:
            if not previous <= word["start"] < word["end"] <= chapter["frames"] / chapter["sample_rate"]:
                raise ValueError(f"Invalid word timing in {path.name}: {word}")
            previous = word["end"]
    return chapters


def lead_time(chapter):
    return Fraction(25 if chapter["id"] == "opening" else 12, 100)


def frame_count(chapter):
    tail = Fraction(90 if chapter["id"] == "end" else 23, 100)
    voice = Fraction(chapter["frames"], chapter["sample_rate"])
    return math.ceil((voice + lead_time(chapter) + tail) * FPS)


def chapter_cues(chapter):
    """Group aligned words at natural pauses and keep the author's timing."""
    pending = []
    words = chapter["words"]
    for index, word in enumerate(words):
        pending.append(word)
        remaining = len(words) - index - 1
        phrase_end = len(pending) >= 3 and word["text"][-1] in ",.?;!"
        if remaining == 0 or (remaining >= 2 and (len(pending) >= 7 or phrase_end)):
            yield (" ".join(w["text"] for w in pending), pending[0]["start"], pending[-1]["end"])
            pending = []


def timestamp(seconds):
    whole, ms = divmod(round(seconds * 1000), 1000)
    minutes, secs = divmod(whole, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{secs:02},{ms:03}"


def captions(chapters):
    blocks = []
    cursor = 0
    for chapter in chapters:
        offset = cursor / FPS + float(lead_time(chapter))
        for text, start, end in chapter_cues(chapter):
            bounds = timestamp(offset + start) + " --> " + timestamp(offset + end)
            blocks.append(f"{len(blocks) + 1}\n{bounds}\n{text}\n")
        cursor += frame_count(chapter)
    return "\n".join(blocks)
