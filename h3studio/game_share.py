"""Publish one saved Game episode as a portable, self-contained reader."""
import base64
import hashlib
import io
import json
import os
import re
import tempfile
import wave
from collections import Counter, defaultdict, deque
from pathlib import Path

from PIL import Image, ImageOps

from . import game_story

MIB = 1024 * 1024
TEMPLATE = Path(__file__).with_name("story_player.html")
PRIVATE = re.compile(
    r"""(?ix)(?:file://\S+|(?:[A-Z]:[\\/]|\\\\[^\\\s]+\\|/(?:Users|home|mnt|tmp|private|var|opt|etc)/)[^\r\n<>"|]*"""
    r"""|\bBearer\s+[\w.~/+=-]{8,}|\b(?:sk-[\w-]{12,}|gh[pousr]_[\w]{16,}|github_pat_[\w]{16,})"""
    r"""|\b(?:api[_-]?key|access[_-]?token|password)\s*[:=]\s*[^\s,;<]+)"""
)


def _public(value):
    return PRIVATE.sub("[private detail]", str(value or "")).strip()


def _jpeg(project, doc, visual):
    revision, asset = visual.get("revision"), visual.get("poster")
    image = None
    if revision:
        try:
            image = project.render_revision(revision)
        except (OSError, ValueError, KeyError, Image.DecompressionBombError):
            if not asset:
                return None
    if image is None and asset:
        with Image.open(project._asset_path(doc, asset)) as source:
            if source.width * source.height > 24_000_000:
                return None
            image = ImageOps.exif_transpose(source).convert("RGBA")
    if image is None:
        return None
    image.thumbnail((960, 640), Image.Resampling.LANCZOS)
    flattened = Image.new("RGB", image.size, "#122125")
    flattened.paste(image, mask=image.getchannel("A"))
    buffer = io.BytesIO()
    flattened.save(buffer, "JPEG", quality=80, optimize=False)
    return buffer.getvalue()


def _speech(project, doc, clip):
    # Copy PCM only. A source WAV can carry ancillary chunks containing local metadata.
    if _public(clip.get("text")) != str(clip.get("text", "")).strip():
        return None
    with wave.open(str(project._asset_path(doc, clip["asset"])), "rb") as source:
        channels, width = source.getnchannels(), source.getsampwidth()
        frames = source.getnframes()
        if channels not in (1, 2) or width not in (1, 2, 3, 4) or not 0 < frames*channels*width <= 2*MIB:
            return None
        pcm = source.readframes(frames)
        if len(pcm) != frames*channels*width:
            return None
        output = io.BytesIO()
        with wave.open(output, "wb") as destination:
            destination.setparams(source.getparams())
            destination.writeframes(pcm)
        return output.getvalue()


def _embed(collection, media_type, data):
    collection.append("data:"+media_type+";base64,"+base64.b64encode(data).decode("ascii"))
    return len(collection)-1


def _episode(project, doc, store, selected, include_voice):
    root = game_story._lineage(store, selected)[0]
    turns = store["turns"]
    authored = turns[root].get("story_node") == "watch" and bool(turns[root].get("story_plates"))
    children = defaultdict(list)
    for ident, turn in turns.items():
        if turn.get("parent"):
            children[turn["parent"]].append(ident)
    voice_for = {}
    if include_voice:
        for clip in doc.get("narrations", {}).values():
            turn_id = clip.get("source", {}).get("turn")
            if turn_id:
                voice_for[turn_id] = clip
    scenes, images, sounds, art_cache = [], [], [], {}
    positions, queue = {root: 0}, deque([(root, None)])
    art_bytes = audio_bytes = missed_art = missed_voice = 0
    while queue:
        ident, inherited = queue.popleft()
        turn = turns[ident]
        node = game_story.RELAY.get(turn.get("story_node")) if authored else None
        title = node["title"] if node else turn.get("action")
        scene = dict(title=_public(title), text=_public(turn.get("scene")), choices=[], art=inherited, voice=None,
                     ending=bool(node and node["ending"]))
        visual = game_story.visual(doc, turn)
        key = (visual.get("revision"), visual.get("poster"))
        if any(key):
            if key not in art_cache:
                art_cache[key] = None
                if art_bytes < 6*MIB:
                    try:
                        raw = _jpeg(project, doc, visual)
                    except (OSError, ValueError, KeyError, Image.DecompressionBombError):
                        raw = None
                    if raw and art_bytes + len(raw) <= 6*MIB:
                        art_cache[key] = _embed(images, "image/jpeg", raw)
                        art_bytes += len(raw)
                if art_cache[key] is None:
                    missed_art += 1
            if art_cache[key] is not None:
                scene["art"] = art_cache[key]
        if clip := voice_for.get(ident):
            try:
                raw = _speech(project, doc, clip) if audio_bytes < 6*MIB else None
            except (OSError, ValueError, KeyError, EOFError, wave.Error):
                raw = None
            if raw and audio_bytes + len(raw) <= 6*MIB:
                scene["voice"] = _embed(sounds, "audio/wav", raw)
                audio_bytes += len(raw)
            else:
                missed_voice += 1
        edges = {}
        if node:
            path = game_story._lineage(store, ident)
            for choice, label in node["choices"]:
                branch = game_story._choose(doc, store, path, {"choice":choice})
                edges[branch["turn"]] = label
        for child in children[ident]:
            edges.setdefault(child, turns[child].get("action", "Continue"))
        labels = {child: _public(label) or "Continue" for child, label in edges.items()}
        counts = Counter(labels.values())
        occurrences = Counter()
        for child, label in labels.items():
            if child not in positions:
                positions[child] = len(positions)
                queue.append((child, scene["art"]))
            if counts[label] > 1:
                occurrences[label] += 1
                label += " · Branch "+str(occurrences[label])
            scene["choices"].append([label, positions[child]])
        scenes.append(scene)
    data = dict(title="Tidal Relay" if authored else _public(turns[root]["action"]),
                kind="Authored interactive story" if authored else "Saved Game branches",
                scenes=scenes, images=images, sounds=sounds)
    return data, dict(scenes=len(scenes), images=len(images), voices=len(sounds),
                      skipped_images=missed_art, skipped_voices=missed_voice)


def export(project, args):
    from .director import _store
    path = Path(args.get("path") or "").expanduser()
    if not path.is_absolute() or path.suffix.lower() != ".html":
        raise ValueError("Choose a full filename ending in .html.")
    path = path.resolve()
    if path.is_relative_to(project.folder) or not path.parent.is_dir():
        raise ValueError("Choose an existing output folder outside the open project.")
    if path.exists():
        raise ValueError("Choose a new filename to preserve the existing file.")
    # Unplayed authored moves are materialized only in this isolated, read-only snapshot.
    doc = project.state()
    store = _store(doc)
    game_story.seed(doc, store)
    data, stats = _episode(project, doc, store, args.get("turn"), args.get("include_voice") is True)
    template = TEMPLATE.read_text("utf-8")
    script = template.split('<script id="reader">', 1)[1].split("</script>", 1)[0]
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode("ascii")
    safe_json = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("&","\\u0026").replace("<","\\u003c").replace(">","\\u003e")
    html = template.replace("READER_HASH", digest).replace("EPISODE_JSON", safe_json)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".partial", delete=False) as stage:
        staged = Path(stage.name)
    try:
        staged.write_bytes(html.encode("utf-8"))
        if os.name == "nt":
            os.rename(staged, path)  # Windows refuses an existing destination.
        else:
            os.link(staged, path)
    except FileExistsError as error:
        raise ValueError("Choose a new filename to preserve the existing file.") from error
    finally:
        staged.unlink(missing_ok=True)
    return dict(path=str(path), **stats)
