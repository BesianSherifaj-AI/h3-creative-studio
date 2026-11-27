"""Shared GPU queue tests use a local mock transport and real SQLite restart."""
import concurrent.futures
import hashlib
import json
import uuid

import httpx
import pytest

from h3studio.jobs import Queue


BASE = "http://127.0.0.1:8010"
GRAPH = {"2": {"class_type": "SaveVideo", "inputs": {}}}


class Renderer:
    def __init__(self):
        self.pending = {}
        self.history = {}
        self.post_ids = []
        self.interrupted = []
        self.drop_ack = False
        self.device = "cuda"

    def handle(self, req):
        path = req.url.path
        if path == "/system_stats":
            return httpx.Response(200, json={"devices": [{"type": self.device, "name": self.device}]})
        if path == "/prompt":
            p = json.loads(req.content)
            ident = p["prompt_id"]
            self.post_ids.append(ident)
            self.pending[ident] = [1, ident, p["prompt"], {"client_id": p["client_id"]}, []]
            if self.drop_ack:
                raise httpx.ReadTimeout("ack dropped", request=req)
            return httpx.Response(200, json={"prompt_id": ident})
        if path == "/queue":
            return httpx.Response(200, json={"queue_running": list(self.pending.values()), "queue_pending": []})
        if path.startswith("/history/"):
            ident = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={ident: self.history[ident]} if ident in self.history else {})
        if path.endswith("/cancel"):
            ident = path.split("/")[-2]
            self.interrupted.append(ident)
            self.pending.pop(ident)
            return httpx.Response(200, json={})
        if path == "/view":
            return httpx.Response(200, content=b"synthetic returned bytes")
        raise AssertionError(path)

    def complete(self, ident):
        self.history[ident] = {"prompt": self.pending.pop(ident), "status": {"completed": True, "status_str": "success"},
                               "outputs": {"2": {"images": [{"filename": "owned.mp4", "subfolder": "owned", "type": "output"}]}}}


@pytest.fixture
def setup_queue(tmp_path):
    renderer = Renderer()
    games = []
    with httpx.Client(transport=httpx.MockTransport(renderer.handle)) as client:
        def restart():
            return Queue(tmp_path, client=client, guard=lambda: games)
        yield renderer, games, restart


def test_game_guard_reservation_and_lost_ack_recovery(setup_queue, tmp_path):
    renderer, games, restart = setup_queue
    q = restart()
    renderer.drop_ack = True
    p = {"comfy_url": BASE, "prompt": GRAPH}
    first = q.enqueue("video", p)["id"]
    (tmp_path/"backend.py").write_text("# acceptance fixture")
    clip = tmp_path/"clip.mp4"
    clip.write_bytes(b"queued only")
    other_jobs = [q.enqueue("image", p)["id"],
                  q.enqueue("director", {"url":BASE,"model":"chosen","prompt":"Opening","mode":"film"})["id"],
                  q.enqueue("finish", {"installation":str(tmp_path),"input":str(clip),"settings":{"workflow":"auto"}})["id"]]
    games.append("RocketLeague.exe")
    q.tick()
    assert not renderer.post_ids
    assert "Game guard" in q.get(first)["message"]
    games.clear()
    q.tick()
    assert q.get(first)["status"] == "uncertain"
    q = restart()
    q.tick()
    assert q.get(first)["status"] == "working"
    assert renderer.post_ids == [first]
    renderer.complete(first)
    q.tick()
    receipt = q.get(first)["receipt"]
    assert receipt["worker"] == BASE and receipt["devices"][0]["type"] == "cuda"
    assert receipt["outputs"][0]["sha256"] == hashlib.sha256(b"synthetic returned bytes").hexdigest()
    assert all(q.get(ident)["status"] == "queued" for ident in other_jobs)
    assert q.get(first)["status"] == "succeeded"


def test_only_cancel_owned_id_and_guarded_worker(setup_queue):
    renderer, games, restart = setup_queue
    q = restart()
    p = {"comfy_url": BASE, "prompt": GRAPH}
    owned = q.enqueue("video", p)["id"]
    foreign = str(uuid.uuid4())
    renderer.pending[foreign] = [0, foreign, GRAPH, {"client_id": "another app"}, []]
    q.tick()
    games.append("RocketLeague.exe")
    restart().tick()
    restart().tick()
    assert renderer.interrupted == [owned] and foreign in renderer.pending
    assert q.get(owned)["status"] == "cancelled"
    with pytest.raises(ValueError, match="Unknown Studio job"):
        q.cancel(foreign)


def test_cpu_worker_is_rejected_and_idempotent_submission(setup_queue):
    renderer, _, restart = setup_queue
    renderer.device = "cpu"
    q = restart()
    p = {"comfy_url": BASE, "prompt": GRAPH}
    ident = str(uuid.uuid4())
    assert q.enqueue("video", p, ident)["id"] == q.enqueue("video", p, ident)["id"]
    with pytest.raises(ValueError, match="another request"):
        q.enqueue("image", p, ident)
    q.tick()
    assert not renderer.post_ids and "CPU mode" in q.get(ident)["message"]
    assert q.get(ident)["status"] == "failed"


def test_racing_desktop_instances_submit_once(setup_queue):
    renderer, _, restart = setup_queue
    q = restart()
    ident = q.enqueue("video", {"comfy_url": BASE, "prompt": GRAPH})["id"]
    with concurrent.futures.ThreadPoolExecutor() as pool:
        futures = [pool.submit(restart().tick) for _ in range(4)]
        for f in futures:
            f.result()
    assert renderer.post_ids == [ident]


def test_before_dispatch_runs_once_and_respects_a_new_game(setup_queue):
    renderer, games, restart = setup_queue
    q = restart()
    peers = []
    def release(url):
        peers.append(url)
        if len(peers) == 1:
            games.append("RocketLeague.exe")
    q.before_dispatch = release
    job = q.enqueue("image", {"comfy_url": BASE, "prompt": GRAPH})["id"]
    q.tick()
    assert peers == [BASE] and not renderer.post_ids and q.get(job)["status"] == "queued"
    games.clear()
    q.tick()
    q.tick()
    assert peers == [BASE, BASE] and renderer.post_ids == [job]


def test_forged_history_does_not_reconcile_or_delete(setup_queue):
    renderer, _, restart = setup_queue
    q = restart()
    ident = q.enqueue("video", {"comfy_url": BASE, "prompt": GRAPH})["id"]
    q.tick()
    renderer.complete(ident)
    renderer.history[ident]["prompt"][3] = {"client_id": "other"}
    q.tick()
    assert q.get(ident)["status"] == "failed"
    assert not renderer.interrupted
