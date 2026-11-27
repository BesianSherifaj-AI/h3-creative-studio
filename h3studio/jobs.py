"""Durable one-job GPU scheduler shared by image, H3, Director and finishing."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path, PurePosixPath

import httpx

from .finish_worker import atomic
from vendor.prompt.resources import local_url


TERMINAL = {"succeeded", "failed", "cancelled"}
GAMES = {"rocketleague.exe", "fortniteclient-win64-shipping.exe", "valorant-win64-shipping.exe",
         "cs2.exe", "overwatch.exe", "r5apex.exe", "cod.exe", "eldenring.exe", "cyberpunk2077.exe"}

class DispatchWaiting(Exception):
    """A live renderer is busy or its queue cannot yet be verified."""


def verify_queue_idle(queue, label):
    if not isinstance(queue,dict) or not all(isinstance(queue.get(k),list) for k in ("queue_running","queue_pending")):
        raise DispatchWaiting(f"Waiting to verify the {label} queue.")
    if queue["queue_running"] or queue["queue_pending"]:
        raise DispatchWaiting(f"Waiting for running or pending work on the {label}.")


def game_guard():
    if os.name != "nt":
        return []
    import psutil
    return sorted({p.info["name"] for p in psutil.process_iter(["name"], ad_value="") if (p.info["name"] or "").lower() in GAMES})


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _download(client, base, params, target):
    tmp, digest = target.with_suffix(target.suffix + ".part"), hashlib.sha256()
    try:
        with client.stream("GET", base + "/view", params=params, timeout=180) as response:
            response.raise_for_status()
            with tmp.open("wb") as out:
                for chunk in response.iter_bytes(1024 * 1024):
                    out.write(chunk)
                    digest.update(chunk)
        if not tmp.stat().st_size:
            raise ValueError("Renderer output was empty.")
        os.replace(tmp, target)
        return digest.hexdigest()
    finally:
        tmp.unlink(missing_ok=True)


class Queue:
    def __init__(self, directory, *, client=None, guard=game_guard, before_dispatch=None, check_occupancy=None):
        self.root = Path(directory).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "jobs.sqlite"
        self.lock = threading.RLock()
        self.client = client
        self.guard = guard
        self.before_dispatch = before_dispatch
        self.check_occupancy = check_occupancy
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                receipt TEXT NOT NULL, message TEXT NOT NULL, cancel INTEGER NOT NULL DEFAULT 0)""")

    @contextlib.contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=25)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _http(self):
        return contextlib.nullcontext(self.client) if self.client is not None else httpx.Client(trust_env=False, timeout=20)

    def enqueue(self, kind, payload, job_id=None):
        if kind not in ("video", "image", "finish", "director"):
            raise ValueError("Unsupported job type.")
        if kind in ("video", "image"):
            local_url(payload["comfy_url"])
            if not isinstance(payload.get("prompt"), dict) or not payload["prompt"]:
                raise ValueError("Choose a prepared Comfy workflow.")
        elif kind == "director":
            local_url(payload["url"])
            if not payload.get("model") or not payload.get("prompt"):
                raise ValueError("Select a Director model and enter a scene brief.")
        else:
            if not Path(payload["installation"], "backend.py").is_file() or not Path(payload["input"]).is_file():
                raise ValueError("Choose a configured UPSCALE installation and a source video.")
        ident = str(uuid.UUID(str(job_id))) if job_id else str(uuid.uuid4())
        data = _canonical(payload)
        now = time.time()
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO jobs VALUES (?,?,?,?,?,?,?,?,0)",
                       (ident, kind, data, "queued", now, now, "{}", "Waiting for renderer"))
            row = db.execute("SELECT kind,payload FROM jobs WHERE id=?", (ident,)).fetchone()
            if row["kind"] != kind or row["payload"] != data:
                raise ValueError("This job identifier already belongs to another request.")
        return self.get(ident)

    def _record(self, row, full=False):
        if row is None:
            raise ValueError("Unknown Studio job.")
        record = {k: row[k] for k in ("id", "kind", "status", "created", "updated", "message")}
        record["receipt"] = json.loads(row["receipt"])
        record["cancel_requested"] = bool(row["cancel"])
        if full:
            record["payload"] = json.loads(row["payload"])
        return record

    def get(self, ident, full=False):
        with self._db() as db:
            return self._record(db.execute("SELECT * FROM jobs WHERE id=?", (ident,)).fetchone(), full)

    def list(self):
        with self._db() as db:
            return [self._record(r) for r in db.execute("SELECT * FROM jobs ORDER BY created DESC")]

    def director_inbox(self, project_id, mode):
        with self._db() as db:
            rows = db.execute("""SELECT id,status,message,json_extract(payload,'$.director_request.brief') AS brief
                FROM jobs WHERE kind='director' AND json_extract(payload,'$.studio_project')=?
                AND json_extract(payload,'$.mode')=? ORDER BY created DESC""",(project_id,mode))
            return [dict(row) for row in rows]

    def _update(self, ident, status, message, receipt=None):
        with self._db() as db:
            db.execute("UPDATE jobs SET status=?, message=?,updated=?,receipt=coalesce(?,receipt) "
                       "WHERE id=? AND status NOT IN ('succeeded','failed','cancelled')",
                       (status, str(message)[:1500], time.time(), None if receipt is None else _canonical(receipt), ident))

    def cancel(self, ident):
        with self._db() as db:
            db.execute("UPDATE jobs SET cancel=1 WHERE id=? AND status NOT IN ('succeeded','failed','cancelled')", (ident,))
            db.execute("UPDATE jobs SET status='cancelled',message='Cancelled before dispatch' WHERE id=? AND status='queued'", (ident,))
        return self.get(ident)

    def _launch(self, job):
        ident, p = job["id"], job["payload"]
        folder = self.root / ident
        folder.mkdir(exist_ok=True)
        if job["kind"] in ("finish", "director"):
            exe = Path(sys.executable)
            if job["kind"] == "finish":
                base = Path(p["installation"])
                exe = base / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
                if not exe.is_file():
                    raise ValueError("Set up the UPSCALE application's own virtual environment first.")
            atomic(folder / "request.json", {"id": ident, "kind": job["kind"], "payload": p})
            proc = subprocess.Popen([str(exe), str(Path(__file__).with_name("finish_worker.py")), str(folder)],
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._update(ident, "working", "Worker started", {"pid": proc.pid})
            return
        base = local_url(p["comfy_url"])
        with self._http() as client:
            stats = client.get(base + "/system_stats")
            stats.raise_for_status()
            devices = stats.json().get("devices", [])
            if not any(str(d.get("type", "")).lower() in ("cuda", "mps", "xpu", "directml", "hip")
                       or str(d.get("name", "")).lower().startswith(("cuda:", "xpu:", "mps:")) for d in devices):
                raise ValueError("This Comfy worker reports CPU mode. Select an active GPU worker.")
            if p.get("packet_master"):
                from .assembly import validate_master
                validate_master(p, client)
            elif p.get("manifest", {}).get("transfer_id"):
                from .generation import validate_saved_take
                validate_saved_take(p, client=client)
            # Graph/packet validation can take time. Recheck peers and guard at handoff.
            if self.check_occupancy:
                self.check_occupancy(base)
            if games := self.guard():
                raise DispatchWaiting("Game guard: " + ", ".join(games))
            if self.get(ident)["cancel_requested"]:
                self._update(ident,"cancelled","Cancelled before dispatch")
                return
            # A configured external target also accepts direct work from other apps.
            try:
                occupancy = client.get(base + "/queue")
                occupancy.raise_for_status()
                verify_queue_idle(occupancy.json(), "selected renderer")
            except (httpx.HTTPError, ValueError) as exc:
                raise DispatchWaiting("Waiting to verify the selected renderer queue.") from exc
            receipt = {"worker": base, "devices": devices, "prompt_id": ident,
                       "submitted_at": time.time(), "accepted": False}
            self._update(ident, "submitting", "Awaiting renderer acknowledgement", receipt)
            response = client.post(base + "/prompt", json={"prompt": p["prompt"], "client_id": ident, "prompt_id": ident,
                                                          "extra_data": {"h3_creative_studio_job": ident}})
            if 400 <= response.status_code < 500:
                raise ValueError("Renderer rejected the workflow: " + response.text[:800])
            response.raise_for_status()
            if response.json().get("prompt_id") != ident:
                self._update(ident, "uncertain", "Renderer did not echo the owned prompt ID; check this worker before retrying.")
                return
            self._update(ident, "working", "Accepted by renderer", {**receipt, "accepted": True})

    def _complete(self, job, history, client):
        p, ident = job["payload"], job["id"]
        base = local_url(p["comfy_url"])
        manifest = p.get("manifest", {})
        allowed = {k for k, n in p["prompt"].items() if n["class_type"] in ("SaveVideo", "SaveImage")}
        if p.get("visual_director") and p["prompt"].get("3", {}).get("class_type")=="SaveText":
            allowed = {"3"}
        outputs, folder = [], self.root / ident
        folder.mkdir(exist_ok=True)
        for node in sorted(allowed):
            for items in history["outputs"].get(node, {}).values():
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict) or item.get("type") != "output":
                        continue
                    name = item.get("filename", "")
                    subfolder = item.get("subfolder", "").replace(chr(92), "/")
                    if not name or name != Path(name).name or ":" in name or ".." in PurePosixPath(subfolder).parts or PurePosixPath(subfolder).is_absolute():
                        raise ValueError("Renderer returned an invalid output name.")
                    meta = {"filename": name, "subfolder": subfolder, "type": "output"}
                    target = folder / f"output-{len(outputs)}{Path(name).suffix}"
                    digest = _download(client, base, meta, target)
                    outputs.append({**meta, "path": str(target), "sha256": digest, "node_id": node})
        if not outputs:
            raise ValueError("Renderer returned no saved output for this graph.")
        packet = None
        media = manifest.get("mmh3", {})
        if media.get("save_enabled"):
            saved = history["outputs"].get(str(media["save_node_id"]), {}).get("mmh3_saved", [])
            if len(saved) != 1:
                raise ValueError("Saved motion output could not be identified.")
            selector = saved[0]["file"]
            if not selector.startswith("output::" + media["output_prefix"]):
                raise ValueError("Saved motion output belongs to a different take.")
            info = client.get(base + "/mmh3_media/file_info", params={"file": selector})
            info.raise_for_status()
            data = info.json()
            if (not data.get("has", {}).get("latent") or not data.get("id") or data.get("revision") is None
                    or any(data.get("geometry", {}).get(k) != manifest.get(k) for k in ("width","height","frames","fps"))):
                raise ValueError("Saved motion packet geometry or identity does not match this take.")
            relative = PurePosixPath(selector.removeprefix("output::").replace(chr(92), "/"))
            if relative.is_absolute() or ".." in relative.parts or ":" in str(relative) or relative.suffix.lower() != ".mmh3":
                raise ValueError("The saved motion packet path is invalid.")
            target = folder / "state.mmh3"
            digest = _download(client, base, {"type": "output", "filename": relative.name, "subfolder": str(relative.parent)}, target)
            packet = {"selector": selector, "packet_id": data["id"], "packet_revision": data["revision"],
                      "path": str(target), "sha256": digest}
        return {**job["receipt"], "accepted": True, "outputs": outputs, "packet": packet}

    def _poll(self, job, guarded):
        ident, p = job["id"], job["payload"]
        cancel = job["cancel_requested"] or bool(guarded)
        if job["kind"] in ("finish", "director"):
            folder = self.root / ident
            if not folder.is_dir():
                self._update(ident, "uncertain", "Worker dispatch has no receipt; check before retrying.")
                return
            if cancel:
                (folder / "cancel").touch()
            if (folder / "result.json").is_file():
                result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
                if result["job_id"] != ident:
                    raise ValueError("Worker receipt belongs to a different job.")
                self._update(ident, result["status"], result.get("error") or "Worker finished", result)
            else:
                import psutil
                pid = job["receipt"].get("pid")
                claim = folder / "claim"
                if claim.is_file():
                    value = claim.read_text().strip()
                    if value.isdigit():
                        pid = int(value)
                if pid:
                    try:
                        proc = psutil.Process(pid)
                        args = proc.cmdline()
                        alive = any(Path(arg).name == "finish_worker.py" for arg in args) and str(folder) in args
                    except psutil.NoSuchProcess:
                        alive = False
                    except psutil.AccessDenied:
                        alive = True
                    if not alive:
                        if (folder / "result.json").is_file():
                            return  # Atomic receipt appeared while we inspected the process.
                        self._update(ident, "failed", "The owned worker exited before writing a result.")
                        return
                elif job["cancel_requested"] and time.time() - job["created"] > 60:
                    self._update(ident, "cancelled", "No live worker or claim was found.")
                    return
                if (folder / "progress.json").is_file():
                    progress = json.loads((folder / "progress.json").read_text(encoding="utf-8"))
                    self._update(ident, "working", progress["message"], {**job["receipt"], **progress})
            return
        base = local_url(p["comfy_url"])
        with self._http() as client:
            reply = client.get(base + "/history/" + ident)
            reply.raise_for_status()
            history = reply.json().get(ident)
            if history:
                record = history.get("prompt")
                if (not isinstance(record, list) or len(record) < 4 or record[1] != ident
                        or not isinstance(record[3], dict) or record[3].get("client_id") != ident):
                    raise ValueError("Renderer history does not identify this Studio request.")
                status = history.get("status", {})
                if status.get("status_str") == "error":
                    messages = status.get("messages", [])
                    interrupted = any(item[0] == "execution_interrupted" for item in messages if isinstance(item, list) and item)
                    self._update(ident, "cancelled" if interrupted else "failed", "Renderer interrupted" if interrupted else str(messages)[-1000:])
                    return
                if status.get("completed"):
                    self._update(ident, "succeeded", "Saved renderer output", self._complete(job, history, client))
                    return
            queue = client.get(base + "/queue")
            queue.raise_for_status()
            found = any(isinstance(row, list) and len(row) >= 4 and row[1] == ident and isinstance(row[3], dict) and row[3].get("client_id") == ident
                        for key in ("queue_running", "queue_pending") for row in queue.json().get(key, []))
            if found and cancel:
                response = client.post(base + "/api/jobs/" + ident + "/cancel")
                response.raise_for_status()
                # The renderer may dequeue a pending job without a history entry.
                self._update(ident, "cancelling", "Cancellation sent to the owned renderer job")
            elif found:
                self._update(ident, "working", "Renderer owns this job", {**job["receipt"], "accepted": True})
            elif job["status"] == "cancelling":
                self._update(ident, "cancelled", "Owned renderer job left the queue")
            elif job["cancel_requested"] and time.time() - job["receipt"].get("submitted_at", job["created"]) > 60:
                # History and queue can change between requests. Re-read history
                # under the same ID before releasing an unacknowledged submission.
                final_lookup = client.get(base + "/history/" + ident)
                final_lookup.raise_for_status()
                if not final_lookup.json().get(ident):
                    self._update(ident, "cancelled", "Confirmed absent from this renderer on explicit cancel.")
            else:
                self._update(ident, "uncertain", "Waiting to reconcile this exact renderer job ID")

    @contextlib.contextmanager
    def _scheduler_lock(self):
        """An OS-held lock also releases when a desktop process crashes."""
        lock = (self.root / "scheduler.lock").open("a+b")
        acquired = False
        try:
            lock.seek(0, 2)
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                pass
            yield acquired
        finally:
            lock.close()

    def tick(self):
        with self.lock, self._scheduler_lock() as acquired:
            if not acquired:
                return
            # An idle Studio never starts a game scan or network request.
            with self._db() as idle:
                if not idle.execute("SELECT 1 FROM jobs WHERE status NOT IN ('succeeded','failed','cancelled') LIMIT 1").fetchone():
                    return
            guard = self.guard()
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM jobs WHERE status NOT IN ('queued','succeeded','failed','cancelled') ORDER BY created LIMIT 1").fetchone()
                fresh = row is None
                if fresh:
                    if guard:
                        db.execute("UPDATE jobs SET message=? WHERE status='queued'", ("Game guard: " + ", ".join(guard),))
                        return
                    row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
                    if row is None:
                        return
            job = self._record(row, True)
            if not fresh and job["status"] == "starting" and not job["receipt"]:
                # No Comfy request can precede its durable 'submitting' receipt.
                # Child workers write request.json before spawn and must reconcile if present.
                if job["kind"] in ("image","video") or not (self.root/job["id"]/"request.json").exists():
                    self._update(job["id"], "cancelled" if job["cancel_requested"] else "queued", "Recovered before dispatch")
                    return
            if fresh and self.get(job["id"])["cancel_requested"]:
                self._update(job["id"], "cancelled", "Cancelled before dispatch")
                return
            try:
                if fresh:
                    if self.before_dispatch:
                        p = job["payload"]
                        active_url = p.get("comfy_url") or p.get("url") or ""
                        self.before_dispatch(active_url)
                        if current_games := self.guard():
                            self._update(job["id"], "queued", "Game guard: " + ", ".join(current_games))
                            return
                        if self.get(job["id"])["cancel_requested"]:
                            self._update(job["id"], "cancelled", "Cancelled before dispatch")
                            return
                    with self._db() as db:
                        reserved = db.execute("UPDATE jobs SET status='starting',message='Dispatch reserved',updated=? "
                                              "WHERE id=? AND status='queued' AND cancel=0",(time.time(),job["id"]))
                        if reserved.rowcount != 1:
                            return
                    self._launch(job)
                else:
                    self._poll(job, guard)
            except DispatchWaiting as exc:
                self._update(job["id"], "queued", str(exc))
            except (httpx.HTTPError, OSError) as exc:
                current = self.get(job["id"])
                state = "failed" if fresh and current["status"] == "starting" else "uncertain"
                self._update(job["id"], state, str(exc))
            except (ValueError, KeyError, TypeError) as exc:
                self._update(job["id"], "failed", str(exc))
