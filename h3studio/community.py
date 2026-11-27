"""Discover media tools or install the pinned Windows build into this Studio home."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath


VERSION = "9.1"
URL = "https://github.com/GyanD/codexffmpeg/releases/download/9.1/ffmpeg-9.1-full_build-shared.zip"
SHA256 = "d010a19b78d5eae62dca8d4c7f195acd56dae8ed6fb85f72821edd9437da2898"
DOWNLOAD_BYTES = 104990867
TOOLS = ("ffmpeg", "ffprobe")
CHUNK = 1024 * 1024


def _root(home):
    return Path(home).resolve() / "tools" / "ffmpeg"


def _private(root):
    try:
        receipt = json.loads((root / "active.json").read_text(encoding="utf-8"))
        name = receipt["folder"]
        if not re.fullmatch(r"9\.1-[a-f0-9]{32}", name) or receipt["sha256"] != SHA256:
            return None
        folder = root / name
        files = receipt["files"]
        if not all(tool + ".exe" in files for tool in TOOLS):
            return None
        if all(Path(k).name == k and (folder / k).stat().st_size == v for k, v in files.items()):
            return folder
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def _system_path(root):
    entries = os.environ.get("PATH", "").split(os.pathsep)
    return [part for part in entries if part and not Path(part).resolve().is_relative_to(root)]


def status(home):
    root = _root(home)
    folder = _private(root)
    system_path = os.pathsep.join(_system_path(root))
    paths = {tool: str(folder / (tool + ".exe")) if folder else shutil.which(tool, path=system_path)
             for tool in TOOLS}
    missing = [tool for tool, path in paths.items() if not path]
    supported = platform.system() == "Windows" and platform.machine().lower() in {"amd64", "x86_64"}
    return {"platform": platform.system(), "media": {**paths, "ready": not missing,
            "source": "private" if folder else "missing" if missing else "system",
            "missing": missing, "can_install": supported, "version": VERSION, "sha256": SHA256,
            "download_url": URL, "download_bytes": DOWNLOAD_BYTES}}


def configure(home):
    root = _root(home)
    folder = _private(root)
    clean = _system_path(root)
    if folder:
        clean.insert(0, str(folder))
    if folder or len(clean) != len([p for p in os.environ.get("PATH", "").split(os.pathsep) if p]):
        os.environ["PATH"] = os.pathsep.join(clean)
    return status(home)


def _check(cancel):
    if cancel and cancel():
        raise InterruptedError("Media tools installation cancelled.")


def _extract(archive, stage, cancel):
    with zipfile.ZipFile(archive) as z:
        files = [f for f in z.infolist() if not f.is_dir()]
        executables = [PurePosixPath(f.filename) for f in files if PurePosixPath(f.filename).parts[-2:] == ("bin","ffmpeg.exe")]
        if len(executables) != 1:
            raise ValueError("The release does not contain the expected FFmpeg program.")
        bin_dir = executables[0].parent
        for entry in files:
            _check(cancel)
            path = PurePosixPath(entry.filename)
            if path.parent != bin_dir or not (path.name in {"ffmpeg.exe","ffprobe.exe"} or path.suffix.lower()==".dll"):
                if path.parent!=bin_dir.parent or not path.name.upper().startswith(("LICENSE", "COPYING")):
                    continue
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", path.name):
                raise ValueError("Unexpected filename in the media tools release.")
            with z.open(entry) as source, (stage / path.name).open("xb") as dest:
                while chunk := source.read(CHUNK):
                    _check(cancel)
                    dest.write(chunk)


def _verify_programs(stage):
    for name in TOOLS:
        result = subprocess.run([str(stage / (name + ".exe")), "-version"], capture_output=True,
                                timeout=30, check=True, creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        if not result.stdout.startswith((name + " version " + VERSION).encode()):
            raise ValueError("The installed " + name + " did not report FFmpeg " + VERSION + ".")


def install(home, cancel=None, progress=None):
    current = configure(home)
    if current["media"]["ready"]:
        return current
    if not current["media"]["can_install"]:
        raise ValueError("Install FFmpeg and FFprobe for this platform, then reopen Studio.")
    root = _root(home)
    root.mkdir(parents=True, exist_ok=True)
    report = progress or (lambda percent, message: None)
    _check(cancel)
    with tempfile.TemporaryDirectory(dir=root, prefix=".install-") as temp:
        scratch = Path(temp)
        archive = scratch / "media.zip"
        digest = hashlib.sha256()
        downloaded = 0
        report(0, "Downloading FFmpeg " + VERSION)
        request = urllib.request.Request(URL, headers={"User-Agent":"H3-Creative-Studio"})
        with urllib.request.urlopen(request, timeout=45) as response, archive.open("xb") as output:
            while chunk := response.read(CHUNK):
                _check(cancel)
                downloaded += len(chunk)
                if downloaded > DOWNLOAD_BYTES:
                    raise ValueError("The media tools download exceeded the published release size.")
                output.write(chunk)
                digest.update(chunk)
                report(round(80 * downloaded / DOWNLOAD_BYTES), "Downloading FFmpeg " + VERSION)
        if digest.hexdigest() != SHA256 or downloaded != DOWNLOAD_BYTES:
            raise ValueError("The FFmpeg download failed the pinned release checksum or size check.")
        report(81, "Extracting verified FFmpeg release")
        stage = scratch / "programs"
        stage.mkdir()
        _extract(archive,stage,cancel)
        _check(cancel)
        report(95, "Checking FFmpeg and FFprobe")
        _verify_programs(stage)
        _check(cancel)
        name = VERSION + "-" + uuid.uuid4().hex
        receipt = {"folder":name, "sha256":SHA256, "source":URL,
                   "files":{f.name:f.stat().st_size for f in stage.iterdir()}}
        pointer = scratch / "active.json"
        pointer.write_text(json.dumps(receipt, indent=2),encoding="utf-8")
        stage.rename(root / name)
        os.replace(pointer, root / "active.json")
    report(100, "FFmpeg and FFprobe are ready")
    return configure(home)
