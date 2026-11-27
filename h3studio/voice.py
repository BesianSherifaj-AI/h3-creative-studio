"""Portable narration clips, film mixing and captions timed from synthesized audio."""
import asyncio
import json
import math
import os
import re
import subprocess
import tempfile
import textwrap
import time
import uuid
import wave
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from vendor.prompt.resources import local_url


def _cancel(check):
    if check():
        raise InterruptedError("Narration cancelled.")


def _sapi(folder, settings, cancelled):
    if os.name != "nt":
        raise ValueError("Windows voices require Windows. Choose a local speech server.")
    system = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    ps = system / "System32/WindowsPowerShell/v1.0/powershell.exe"
    request = folder / "request.json"
    request.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
    script = Path(__file__).with_name("voice-sapi.ps1")
    with (folder / "speech.log").open("wb") as log:
        process = subprocess.Popen([str(ps), "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                                    "Bypass", "-File", str(script), "-RequestFile", str(request)],
                                   stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + 240
        try:
            while process.poll() is None:
                _cancel(cancelled)
                if time.monotonic() > deadline:
                    raise ValueError("Windows speech timed out. Try a shorter passage.")
                time.sleep(0.08)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
    if process.returncode:
        detail = (folder / "speech.log").read_text(errors="replace").splitlines()
        raise ValueError("Windows speech failed. " + (detail[0][:240] if detail else "Check the installed voice."))


@lru_cache(maxsize=1)
def voices():
    if os.name != "nt":
        return []
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        _sapi(folder, {"mode": "voices"}, lambda: False)
        return json.loads((folder / "voices.json").read_text(encoding="utf-8-sig"))


def _string(value, label, maximum, *, required=True):
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise ValueError(f"Enter {label} using up to {maximum} characters.")
    if any(ord(ch) < 32 and ch not in "\r\n\t" for ch in value):
        raise ValueError(f"Remove control characters from {label}.")
    return value.strip()


def _number(value, lo, hi, label):
    try:
        num = float(value)
    except (ValueError, TypeError):
        num = math.nan
    if isinstance(value, bool) or not lo <= num <= hi:
        raise ValueError(f"{label} must be between {lo} and {hi}.")
    return num


def _endpoint(address):
    url = local_url(str(address).strip())
    parsed = urlsplit(url)
    if parsed.path.rstrip("/") not in ("", "/v1", "/v1/audio/speech"):
        raise ValueError("Use the local server address or its /v1/audio/speech endpoint.")
    return f"http://{parsed.netloc}/v1/audio/speech"


def server_models(address):
    url = _endpoint(address).removesuffix("/audio/speech") + "/models"
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=4.0) as client:
        try:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes(65536):
                    if len(body) + len(chunk) > 1 << 20:
                        raise ValueError("The model list is too large.")
                    body.extend(chunk)
        except httpx.HTTPError as exc:
            raise ValueError("The local server could not list models. You can enter the model and voice directly.") from exc
    listing = json.loads(body)
    if not isinstance(listing, dict) or not isinstance(listing.get("data"), list):
        raise ValueError("The server returned an unsupported model list.")
    models = []
    for entry in listing["data"][:1000]:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and 0 < len(entry["id"]) <= 180:
            names = entry.get("voices")
            models.append(dict(id=entry["id"], voices=[v for v in names[:500] if isinstance(v, str) and 0 < len(v) <= 160]
                               if isinstance(names, list) else []))
    return models


def _script_segments(text):
    groups = re.split(r"\n+|(?<=[.!?。！？])\s+", text)
    segments = [line for group in groups for line in textwrap.wrap(" ".join(group.split()),
                width=190, break_on_hyphens=False)]
    if not 1 <= len(segments) <= 96:
        raise ValueError("Use between 1 and 96 short caption passages per narration.")
    return segments


def _pronunciations(args):
    entries = args.get("pronunciations", {})
    if not isinstance(entries, dict) or len(entries) > 24:
        raise ValueError("Use a pronunciation dictionary with up to 24 entries.")
    result = {}
    for original, spoken in entries.items():
        original = _string(original, "a written name", 64)
        spoken = _string(spoken, "a spoken name", 96)
        if any(char in original + spoken for char in "\r\n="):
            raise ValueError("Use one written name and spoken name per pronunciation.")
        result[original] = spoken
    return result


def _spoken_segments(segments, entries):
    if not entries:
        return segments
    names = "|".join(re.escape(name) for name in sorted(entries, key=len, reverse=True))
    pattern = re.compile(r"(?<!\w)(?:" + names + r")(?!\w)")
    result = [pattern.sub(lambda match:entries[match.group()], line) for line in segments]
    if any(len(line) > 700 for line in result):
        raise ValueError("The pronunciation guide makes a passage too long. Shorten its replacements.")
    return result


def _spec(doc, args):
    text = _string(args.get("text"), "the script", 6000)
    speaker = _string(args.get("speaker", "Narrator"), "a speaker name", 100)
    title = _string(args.get("title") or text[:68], "a clip title", 120)
    engine = args.get("engine", "windows")
    if engine not in ("windows", "local"):
        raise ValueError("Choose Windows voices or a local speech server.")
    voice = _string(args.get("voice"), "a voice name", 160)
    rate = _number(args.get("rate", 1), 0.5, 2.0, "Speaking speed")
    source = {}
    for key, collection in (("shot", doc["shots"]), ("take", doc["takes"]),
                            ("turn", doc.get("director", {}).get("turns", {}))):
        value = args.get(key)
        if value:
            if not isinstance(value, str) or value not in collection:
                raise ValueError(f"The source {key} is missing from this project.")
            source[key] = value
    if source.get("shot") and source.get("take") and doc["takes"][source["take"]]["shot"] != source["shot"]:
        raise ValueError("The selected take belongs to a different shot.")
    settings = dict(title=title, text=text, speaker=speaker, engine=engine, voice=voice, rate=rate,
                    source=source, pronunciations=_pronunciations(args))
    if engine == "local":
        settings.update(endpoint=_endpoint(args.get("endpoint", "")),
                        model=_string(args.get("model"), "a local model name", 180))
    elif voice not in {item["name"] for item in voices()}:
        raise ValueError("That Windows voice is unavailable. Choose an installed desktop voice.")
    return settings


async def _receive_audio(folder, spec, segments, cancelled, report):
    total_bytes = 0
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=httpx.Timeout(120, connect=4)) as client:
        for i, line in enumerate(segments):
            _cancel(cancelled)
            payload = dict(model=spec["model"], voice=spec["voice"], speed=spec["rate"],
                           input=line, response_format="wav")
            report(round(i * 85 / len(segments)), f"Speaking passage {i + 1} of {len(segments)}")
            try:
                async with client.stream("POST", spec["endpoint"], json=payload) as response:
                    if response.status_code != 200:
                        raise ValueError(f"Local speech server returned HTTP {response.status_code}. Check the model and voice.")
                    with (folder / f"{i}.wav").open("wb") as out:
                        async for chunk in response.aiter_bytes(65536):
                            _cancel(cancelled)
                            total_bytes += len(chunk)
                            if total_bytes > 128 << 20:
                                raise ValueError("Local narration exceeds the clip size limit.")
                            out.write(chunk)
            except httpx.HTTPError as exc:
                raise ValueError(f"Could not receive speech from the local server ({type(exc).__name__}).") from exc


def _server_audio(folder, spec, segments, cancelled, report):
    # Check cancellation even while the server is still computing its first byte.
    async def receive():
        task = asyncio.create_task(_receive_audio(folder, spec, segments, cancelled, report))
        deadline = time.monotonic() + 300
        try:
            while not task.done():
                _cancel(cancelled)
                if time.monotonic() >= deadline:
                    raise ValueError("The local speech server exceeded the five-minute clip limit.")
                await asyncio.wait({task}, timeout=0.15)
            return task.result()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    return asyncio.run(receive())


def _join(folder, segments, output, cancelled):
    cues = []
    cursor = 0
    shape = None
    with wave.open(str(output), "wb") as target:
        # Leave a valid, empty header if the first returned file fails validation.
        target.setparams((1, 2, 24000, 0, "NONE", ""))
        for i, line in enumerate(segments):
            _cancel(cancelled)
            try:
                source = wave.open(str(folder / f"{i}.wav"), "rb")
            except (EOFError, wave.Error) as exc:
                raise ValueError("The speech engine must return an uncompressed PCM WAV.") from exc
            with source:
                channels, width, hz, frames = source.getparams()[:4]
                if (source.getcomptype() != "NONE" or width != 2 or channels not in (1, 2)
                        or not 8000 <= hz <= 96000 or not 0 < frames <= hz * 600):
                    raise ValueError("Use a 16-bit mono or stereo PCM WAV between 8 and 96 kHz.")
                if shape is None:
                    shape = (channels, width, hz)
                    target.setparams((*shape, 0, "NONE", ""))
                if (channels, width, hz) != shape:
                    raise ValueError("The local voice changed audio format between passages.")
                if i:
                    gap = round(hz * 0.10)
                    target.writeframesraw(bytes(gap * width * channels))
                    cursor += gap
                if cursor + frames > hz * 900:
                    raise ValueError("Keep each narration clip under 15 minutes.")
                left = frames
                while left:
                    _cancel(cancelled)
                    count = min(left, 65536)
                    data = source.readframes(count)
                    if len(data) != count * width * channels:
                        raise ValueError("The speech engine returned an incomplete PCM WAV.")
                    target.writeframesraw(data)
                    left -= count
                cues.append(dict(text=line, start=cursor, end=cursor + frames))
                cursor += frames
    return dict(cues=cues, frames=cursor, sample_rate=shape[2], duration=cursor / shape[2])


def render(project, args, cancelled=lambda: False, report=lambda percent, message: None):
    _cancel(cancelled)
    spec = _spec(project.state(), args)
    segments = _script_segments(spec["text"])
    spoken = _spoken_segments(segments, spec["pronunciations"])
    with tempfile.TemporaryDirectory(prefix="h3-voice-") as tmp:
        folder = Path(tmp)
        if spec["engine"] == "windows":
            report(5, f"Speaking with {spec['voice']}")
            _sapi(folder, dict(mode="render", voice=spec["voice"], rate=round(10 * math.log2(spec["rate"])),
                               segments=spoken), cancelled)
        else:
            _server_audio(folder, spec, spoken, cancelled, report)
        report(90, "Saving narration and caption timings")
        file = folder / "narration.wav"
        info = _join(folder, segments, file, cancelled)
        with file.open("rb") as stream:
            asset = project.store_stream(stream, "Narration.wav", cancelled=cancelled)
        clip = dict(id=uuid.uuid4().hex, asset=asset["id"], **spec, **info)
        with project.transaction() as doc:
            _cancel(cancelled)
            doc["assets"].setdefault(asset["id"], asset)
            doc.setdefault("narrations", {})[clip["id"]] = clip
    return clip


def place(project, args):
    offset = _number(args.get("offset", 0), 0, 86400, "Timeline offset")
    gain = _number(args.get("gain_db", 0), -96, 24, "Gain")
    track = args.get("track", 0)
    if type(track) is not int or not 0 <= track < 8:
        raise ValueError("Choose an output track between 1 and 8.")
    with project.transaction() as doc:
        clip = _clip(doc, args.get("id"))
        ident = "voice-" + clip["id"]
        bed = dict(id=ident, asset=clip["asset"], name=clip["title"], stream=0,
                   track=track, gain_db=gain, offset=offset)
        doc.setdefault("soundtracks", {})[ident] = bed
        return bed


def _clip(doc, ident):
    clip = doc.get("narrations", {}).get(ident)
    if not clip:
        raise ValueError("Choose a saved narration from this project.")
    return clip


def _timestamp(ms):
    seconds, millis = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def caption_rows(doc, ident=None):
    clips = doc.get("narrations", {})
    selected = [_clip(doc, ident)] if ident else list(clips.values())
    rows = []
    for item in selected:
        bed = doc.get("soundtracks", {}).get("voice-" + item["id"])
        # An unplaced clip can export a standalone sidecar starting at zero.
        if bed and bed['asset'] != item['asset']:
            bed = None
        if not bed and not ident:
            continue
        offset = (bed or {}).get("offset", 0)
        for cue in item["cues"]:
            start, end = (round((offset + cue[k] / item["sample_rate"]) * 1000) for k in ("start", "end"))
            line = cue["text"]
            if item["speaker"].casefold() != "narrator":
                line = f"{item['speaker']}: {line}"
            rows.append((start, end, line))
    return sorted(rows)


def captions(doc, ident=None):
    rows = caption_rows(doc, ident)
    if not rows:
        raise ValueError("Place narration on the timeline before exporting film captions.")
    lines = [f"{i}\n{_timestamp(a)} --> {_timestamp(b)}\n{line}\n"
             for i, (a, b, line) in enumerate(rows, 1)]
    return "\n".join(lines)


def action(studio, project, op, args):
    if op == "voice_models":
        return dict(models=server_models(args.get("endpoint")))
    if op == "voice_list":
        try:
            installed, warning = voices(), ""
        except (OSError, ValueError) as exc:
            installed, warning = [], str(exc)
        return dict(voices=installed, warning=warning)
    if op == "voice_render":
        return studio._background("Generate narration",
                                  lambda cancel, report: render(project, args, cancel, report), with_progress=True)
    if op == "voice_place":
        return place(project, args)
    if op == "voice_captions":
        text = captions(project.state(), args.get("id"))
        if not args.get("path"):
            return dict(srt=text)
        path = Path(args["path"])
        if path.suffix.lower() != ".srt":
            raise ValueError("Choose a filename ending in .srt.")
        if path.exists():
            raise ValueError("Choose a new caption filename to preserve the existing file.")
        # Exclusive creation also protects against a racing second exporter.
        with path.open("x", encoding="utf-8", newline="\n") as file:
            file.write(text)
        return dict(path=str(path))
    raise ValueError("Unknown narration operation.")
