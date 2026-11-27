"""Portable H3 projects with immutable take/revision snapshots and hashed media."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import uuid
import zipfile
from contextlib import closing, contextmanager

from PIL import Image, ImageChops, ImageOps
from vendor.atelier.layers import blend_layer, transformed_layer


def _uid():
    return uuid.uuid4().hex


def _lookup(state, group, identifier):
    try:
        return state[group][identifier]
    except (KeyError, TypeError):
        raise ValueError(f"Unknown {group} ID: {identifier}") from None


def _finite(value, low, high, label):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{label} must be from {low} to {high}.")


def _size(width, height):
    if type(width) is not int or type(height) is not int or min(width, height) < 1 or max(width, height) > 8192 or width * height > 24_000_000:
        raise ValueError("Canvas supports up to 8192 pixels per side and 24 megapixels.")


def _hash(handle):
    return hashlib.file_digest(handle, "sha256").hexdigest()


def packet_info(path, expected=None):
    """Verify the primary motion resource's identity, provenance and render geometry."""
    expected = expected or {}
    try:
        with zipfile.ZipFile(path) as packet:
            if packet.getinfo("packet.json").file_size > 16 * 1024 * 1024:
                raise ValueError("Oversized packet manifest.")
            manifest = json.loads(packet.read("packet.json"))
            primary = manifest["primary"]["latent"]
            latents = [r for r in manifest["resources"] if r.get("kind") == "latent" and r.get("id") == primary]
            if len(latents) != 1:
                raise ValueError("The packet must contain one primary motion latent.")
            latent = latents[0]
            contract = latent["extensions"]["minimax_h3"]["latent"]
            if (contract.get("contract_version") != 2 or contract.get("model_family") != "minimax_h3" or
                    contract.get("layout") != "joint_av" or contract.get("origin") != "sampler_output" or contract.get("batch") != 1 or
                    contract["timeline"].get("temporal_grid") != "h3_causal_17k5" or contract["timeline"].get("fps") != 24 or
                    any(contract["binding"].get(key) != "bound" for key in ("time", "geometry")) or
                    contract["timeline"].get("absolute_start_frame") is None):
                raise ValueError("Motion packet has no bound sampler-output lineage.")
            geometry = dict(contract["canvas"], frames=contract["timeline"]["frames"])
            if any(expected[key] != geometry[key] for key in ("width", "height", "frames") if expected.get(key) is not None):
                raise ValueError("Motion packet geometry does not match the take.")
            frames = geometry["frames"]
            if type(frames) != int or frames < 5 or (frames-5)%17:
                raise ValueError("Motion packet uses an invalid frame grid.")
            shapes = {"video":[1,24,2+5*((frames-5)//17),geometry["height"]//16,geometry["width"]//16],
                      "audio":[1,32,2,round(frames*40/24)]}
            with packet.open(latent["path"]) as stream:
                prefix = stream.read(8)
                length = int.from_bytes(prefix,"little")
                if len(prefix)!=8 or not 2 <= length <= 16<<20:
                    raise ValueError("Invalid safetensors header in motion packet.")
                header = stream.read(length)
                tensors = json.loads(header)
                available = packet.getinfo(latent["path"]).file_size - 8 - length
                for name,shape in shapes.items():
                    tensor = tensors[name]
                    start,end = tensor["data_offsets"]
                    width = {"F32":4,"F16":2,"BF16":2,"F64":8}.get(tensor["dtype"])
                    if (contract["streams"][name]["shape"] != shape or tensor["shape"] != shape or width is None
                            or not 0 <= start < end <= available or end-start != math.prod(shape)*width):
                        raise ValueError("Motion packet stream dimensions do not match the motion contract.")
                hashed = hashlib.sha256(prefix + header)
                while chunk := stream.read(1<<20):
                    hashed.update(chunk)
                digest = hashed.hexdigest()
            if latent["content"]["digest"] != "sha256:" + digest:
                raise ValueError("Motion packet latent checksum mismatch.")
            if expected.get("packet_id") and expected["packet_id"] != manifest["id"]:
                raise ValueError("Motion packet identity does not match the take.")
            seed = manifest.get("generation", {}).get("seed")
            if expected.get("seed") is not None and seed is not None and str(expected["seed"]) != str(seed):
                raise ValueError("Motion packet seed does not match the take.")
            return {"id": manifest["id"], "latent": latent["id"], "sha256": digest,
                    "revision": latent["content"].get("revision"), **geometry}
    except (OSError, zipfile.BadZipFile, KeyError, TypeError, IndexError) as e:
        raise ValueError("Unreadable or incomplete MiniMax H3 motion packet.") from e


class Project:
    DATABASE = "studio.h3project"

    def __init__(self, folder):
        supplied = Path(folder).expanduser().resolve()
        self.folder = supplied.parent if supplied.is_file() and supplied.name == self.DATABASE else supplied
        self.database = self.folder / self.DATABASE
        if not self.database.is_file() or self.state().get("version") != 1:
            raise ValueError("Choose an H3 Creative Studio project folder.")

    @classmethod
    def create(cls, folder, name):
        folder = Path(folder).expanduser().resolve()
        folder.mkdir(parents=True, exist_ok=True)
        database = folder / cls.DATABASE
        # Exclusive creation avoids mistaken overwrites and racing two launchers.
        with database.open("xb"):
            pass
        state = {key: {} for key in ("assets", "compositions", "revisions", "shots", "takes", "legacy", "missing")}
        state.update(version=1, id=_uid(), name=str(name).strip() or "Untitled film", cut=[], cut_history=[])
        try:
            with closing(sqlite3.connect(database)) as db, db:
                db.execute("CREATE TABLE project (id INTEGER PRIMARY KEY, document TEXT NOT NULL)")
                db.execute("INSERT INTO project VALUES (1, ?)", (json.dumps(state),))
            (folder / "assets").mkdir(exist_ok=True)
        except BaseException:
            database.unlink(missing_ok=True)
            raise
        return cls(folder)

    def _connect(self):
        return sqlite3.connect(self.database.as_uri() + "?mode=rw", uri=True, timeout=45)

    def state(self):
        with closing(self._connect()) as db:
            return json.loads(db.execute("SELECT document FROM project WHERE id=1").fetchone()[0])

    @contextmanager
    def transaction(self):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            state = json.loads(db.execute("SELECT document FROM project WHERE id=1").fetchone()[0])
            yield state
            raw = json.dumps(state, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            db.execute("UPDATE project SET document=? WHERE id=1", (raw,))

    def store_stream(self, stream, filename, expected_hash=None, *, cancelled=None):
        """Stage a blob before referencing it in a transaction. Safe to retry after interruption."""
        base = self.folder / "assets"
        base.mkdir(exist_ok=True)
        fd, name = tempfile.mkstemp(dir=base, suffix=".partial")
        tmp = Path(name)
        try:
            h = hashlib.sha256()
            with os.fdopen(fd, "wb") as dst:
                while chunk := stream.read(1 << 20):
                    if cancelled and cancelled():
                        raise InterruptedError("Asset import cancelled.")
                    h.update(chunk)
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            digest = h.hexdigest()
            if expected_hash and digest != expected_hash:
                raise ValueError("Selected file checksum does not match the saved asset.")
            ext = Path(filename).suffix.lower()
            if not re.fullmatch(r"\.[a-z0-9]{1,10}", ext):
                ext = ".bin"
            target = base / digest[:2] / (digest + ext)
            target.parent.mkdir(exist_ok=True)
            if not target.exists():
                tmp.replace(target)
            return {"id": digest, "sha256": digest, "name": Path(filename).name, "size": target.stat().st_size,
                    "path": target.relative_to(self.folder).as_posix()}
        finally:
            tmp.unlink(missing_ok=True)

    def add_asset(self, path):
        path = Path(path)
        with path.open("rb") as stream:
            record = self.store_stream(stream, path.name)
        with self.transaction() as state:
            state["assets"].setdefault(record["id"], record)
        return record["id"]

    def asset_path(self, asset_id):
        return self._asset_path(self.state(), asset_id)

    def _asset_path(self, state, asset_id):
        record = _lookup(state, "assets", asset_id)
        path = (self.folder / record["path"]).resolve()
        if not path.is_relative_to(self.folder / "assets") or not path.is_file():
            raise ValueError(f"Missing project asset: {record['name']}")
        return path

    def save_composition(self, name, width, height, layers, composition_id=None, *, base_revision=None, job_id=None, generation=None):
        _size(width, height)
        with self.transaction() as state:
            if job_id and job_id in state.get("image_jobs",{}):
                return state["image_jobs"][job_id]
            if composition_id:
                entry = _lookup(state, "compositions", composition_id)
                if base_revision and entry["current"] != base_revision:
                    raise ValueError("The composition changed. Reload before saving this edit.")
            else:
                composition_id = _uid()
                entry = state["compositions"][composition_id] = {"id": composition_id, "revisions": [], "current": None}
            if not isinstance(layers, list) or len(layers) > 128:
                raise ValueError("Use up to 128 layers.")
            for layer in layers:
                _lookup(state, "assets", layer.get("asset"))
                if layer.get("mask"):
                    _lookup(state, "assets", layer["mask"])
                for field, default, low, high in (("x",0,-8192,8192),("y",0,-8192,8192),("rotation",0,-360,360),
                                                  ("scale",1,.05,4),("opacity",1,0,1)):
                    _finite(layer.get(field, default), low, high, field)
                if layer.get("blend", "normal") not in ("normal","multiply","screen","overlay","darken","lighten","difference"):
                    raise ValueError("Unknown layer blend mode.")
            rev = _uid()
            state["revisions"][rev] = {"id": rev, "composition": composition_id, "name": str(name), "width": width,
                                       "height": height, "parent": entry["current"], "layers": layers}
            entry.update(name=str(name), current=rev)
            entry["revisions"].append(rev)
            if job_id:
                state.setdefault("image_jobs",{})[job_id] = rev
            if generation is not None:
                state["revisions"][rev]["generation"] = generation
        return rev

    def render_revision(self, revision_id):
        state = self.state()
        revision = _lookup(state, "revisions", revision_id)
        _size(revision["width"], revision["height"])
        canvas = Image.new("RGBA", (revision["width"], revision["height"]))
        for layer in revision["layers"]:
            if not layer.get("visible", True):
                continue
            with Image.open(self._asset_path(state, layer["asset"])) as handle:
                _size(*handle.size)
                original = ImageOps.exif_transpose(handle).convert("RGBA")
            if layer.get("mask"):
                with Image.open(self._asset_path(state, layer["mask"])) as handle:
                    mask = ImageOps.exif_transpose(handle).convert("L")
                if mask.size != original.size:
                    raise ValueError("A layer mask must match its source dimensions.")
                original.putalpha(ImageChops.multiply(original.getchannel("A"), mask))
            image, origin = transformed_layer(layer, lambda _: original)
            blend_layer(canvas, image, origin, layer.get("blend", "normal"))
        return canvas

    @staticmethod
    def _shot(name, duration):
        _finite(duration, 1 / 24, 86400, "Duration")
        return {"id": _uid(), "name": str(name), "duration": duration, "guides": [], "takes": [], "selected_take": None}

    def add_shot(self, name, duration=5):
        shot = self._shot(name, duration)
        with self.transaction() as state:
            state["shots"][shot["id"]] = shot
            state["cut"].append(shot["id"])
        return shot["id"]

    def set_guides(self, shot_id, guides):
        with self.transaction() as state:
            shot = _lookup(state, "shots", shot_id)
            if not isinstance(guides, list):
                raise ValueError("Use an ordered list of guides.")
            for guide in guides:
                frame = guide.get("frame")
                if type(frame) is not int or not 0 <= frame < round(24 * shot["duration"]):
                    raise ValueError("Guide frame is outside the delivered shot.")
                refs = [(group, guide[key]) for key, group in (("revision","revisions"),("asset","assets"),("audio_asset","assets")) if guide.get(key)]
                if not refs:
                    raise ValueError("A guide needs a saved composition revision or a media asset.")
                for group, identifier in refs:
                    _lookup(state, group, identifier)
            shot["guides"] = guides

    @staticmethod
    def _remember_cut(state):
        snapshot = [{"shot": s, "take": state["shots"][s]["selected_take"]} for s in state["cut"]]
        if snapshot and (not state["cut_history"] or state["cut_history"][-1] != snapshot):
            state["cut_history"].append(snapshot)

    def save_take(self, shot_id, settings, video_asset=None, packet_asset=None, parent_take=None, *, guides=None):
        if not isinstance(settings, dict):
            raise ValueError("Take settings must be an object.")
        with self.transaction() as state:
            shot = _lookup(state, "shots", shot_id)
            if job_id := settings.get("job_id"):
                for existing in state["takes"].values():
                    if existing["settings"].get("job_id") == job_id:
                        return existing["id"]
            parent_take = parent_take or shot.get("parent_take")
            if parent_take:
                _lookup(state, "takes", parent_take)
            if video_asset:
                self._asset_path(state, video_asset)
            packet = packet_info(self._asset_path(state, packet_asset), settings) if packet_asset else None
            ident = _uid()
            self._remember_cut(state)
            state["takes"][ident] = {"id": ident, "shot": shot_id, "settings": settings, "guides": copy.deepcopy(shot["guides"] if guides is None else guides),
                                     "video_asset": video_asset, "packet_asset": packet_asset, "packet": packet,
                                     "parent_take": parent_take, "trim": self._take_trim(settings), "duration": shot["duration"]}
            shot["takes"].append(ident)
            shot["selected_take"] = ident
            self._mark_dependents(state, shot_id, ident)
        return ident

    @staticmethod
    def _take_trim(settings):
        # H3 guide positions address the native 24 fps render, before delivery trimming.
        seconds = settings.get("delivery_trim")
        if seconds is not None:
            start, end = seconds
            if not 0 <= start < end or not math.isfinite(end):
                raise ValueError("Invalid delivery trim.")
            return {"start_frame":math.floor(24*start+1e-9), "end_frame":math.ceil(24*end-1e-9)-1,
                    "seconds":[start,end]}
        if settings.get("trim") is not None:
            return settings["trim"]
        if native := settings.get("render_trim"):
            return {"start_frame":native[0], "end_frame":native[1]-1}

    @staticmethod
    def _mark_dependents(state, shot_id, take_id):
        for child in state["shots"].values():
            parent = state["takes"].get(child.get("parent_take"), {})
            if parent.get("shot") == shot_id:
                child["needs_review"] = parent["id"] != take_id

    def select_take(self, shot_id, take_id):
        with self.transaction() as state:
            shot = _lookup(state, "shots", shot_id)
            if take_id not in shot["takes"]:
                raise ValueError("The take does not belong to this shot.")
            self._remember_cut(state)
            shot["selected_take"] = take_id
            self._mark_dependents(state, shot_id, take_id)

    def branch_take(self, take_id, action, name=None):
        if action not in ("continue", "retry", "scene"):
            raise ValueError("Choose continue, retry, or scene.")
        with self.transaction() as state:
            take = _lookup(state, "takes", take_id)
            old = _lookup(state, "shots", take["shot"])
            shot = self._shot(name or (old["name"] + " · " + action), old["duration"])
            shot.update(branch_action=action)
            if action == "retry":
                shot.update(parent_take=take_id, guides=copy.deepcopy(take["guides"]), settings=copy.deepcopy(take["settings"]))
            if action == "continue":
                s = take["settings"]
                trim = self._take_trim(s) or take.get("trim")
                shown = trim.get("end_frame") if isinstance(trim, dict) else None
                if shown is None and isinstance(s.get("render_trim"),list) and len(s["render_trim"]) == 2:
                    shown = s["render_trim"][1]-1
                if shown is None:
                    shown = s.get("overlap_frames", 0) + round(take["duration"] * 24) - 1
                matches = bool(take.get("packet") and shown == take["packet"]["frames"] - 1 and not s.get("edited_ending"))
                shot.update(parent_take=take_id, parent_packet=take.get("packet_asset"), parent_trim=trim,
                            parent_frame=shown, start_mode="motion" if matches else "choose_ending",
                            start_modes=["visual", "regenerate"] + (["original_motion"] if take.get("packet") else []))
            state["shots"][shot["id"]] = shot
            self._remember_cut(state)
            if action == "retry" and old["id"] in state["cut"]:
                state["cut"][state["cut"].index(old["id"])] = shot["id"]
            else:
                index = state["cut"].index(old["id"]) + 1 if action == "continue" and old["id"] in state["cut"] else len(state["cut"])
                state["cut"].insert(index, shot["id"])
        return shot["id"]

    def choose_start(self, shot_id, mode, revision=None):
        """Record the explicit choice if the displayed ending differs from saved motion."""
        with self.transaction() as state:
            shot = _lookup(state,"shots",shot_id)
            if shot.get("branch_action") != "continue" or mode not in shot.get("start_modes",[]):
                raise ValueError("That continuation start is unavailable.")
            if revision:
                _lookup(state,"revisions",revision)
            if mode in ("visual","regenerate") and not revision:
                parent = _lookup(state,"takes",shot["parent_take"])
                if not parent.get("video_asset") in state["assets"]:
                    raise ValueError("Relink the displayed video or select an edited ending revision.")
            shot.update(start_mode=mode,start_revision=revision)
            if revision and mode in ("visual","regenerate"):
                shot["guides"] = [{"frame":0,"revision":revision,"role":"image"}] + [g for g in shot["guides"] if g.get("frame") != 0]

    def _apply_cut(self, state, entries):
        if not entries:
            raise ValueError("The selected cut is empty.")
        for entry in entries:
            shot = _lookup(state,"shots",entry["shot"])
            if entry.get("take") and entry["take"] not in shot["takes"]:
                raise ValueError("A take in this cut needs to be recovered.")
        self._remember_cut(state)
        state["cut"] = [entry["shot"] for entry in entries]
        for entry in entries:
            state["shots"][entry["shot"]]["selected_take"] = entry.get("take")
            self._mark_dependents(state,entry["shot"],entry.get("take"))

    def restore_cut(self, index):
        with self.transaction() as state:
            if type(index) != int or not 0 <= index < len(state["cut_history"]):
                raise ValueError("Unknown saved cut.")
            self._apply_cut(state,state["cut_history"][index])

    def open_legacy_cut(self, key):
        with self.transaction() as state:
            cuts = state.get("legacy",{}).get("cuts",{})
            if key not in cuts:
                raise ValueError("Unknown recovered cut.")
            entries = []
            seen = set()
            for index,item in enumerate(cuts[key]):
                if isinstance(item,str):
                    entries.append({"shot":item,"take":_lookup(state,"shots",item)["selected_take"]})
                else:
                    take = _lookup(state,"takes",item["take"])
                    shot_id = take["shot"]
                    if shot_id in seen:
                        # The same source shot can appear with different takes at two cut positions.
                        occurrence = uuid.uuid5(uuid.NAMESPACE_URL,f"{state['id']}/{key}/{index}/{shot_id}").hex
                        if occurrence not in state["shots"]:
                            state["shots"][occurrence] = dict(copy.deepcopy(state["shots"][shot_id]),id=occurrence,source_shot=shot_id)
                        shot_id = occurrence
                    seen.add(shot_id)
                    entries.append({"shot":shot_id,"take":take["id"]})
            self._apply_cut(state,entries)
