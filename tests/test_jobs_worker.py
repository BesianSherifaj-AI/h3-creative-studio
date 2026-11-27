"""Exercise the isolated interpreter protocol without GPU models or external services."""
import hashlib
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from h3studio.finish_worker import direct
from h3studio.jobs import Queue, TERMINAL


def poll(queue, ident, predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        queue.tick()
        result = queue.get(ident)
        if predicate(result):
            return result
        time.sleep(0.03)
    raise AssertionError(queue.get(ident))


def test_isolated_finish_auto_manual_progress_cancel_and_ownership(tmp_path):
    backend = tmp_path / "installed"
    backend.mkdir()
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(backend / ".venv")], check=True)
    (backend / "backend.py").write_text("""
from pathlib import Path
class Settings:
    def __init__(self, workflow): self.workflow = workflow

def process_video(source, settings, directory, progress, cancel):
    directory.mkdir()
    progress(0.5, "Processing owned fixture")
    if settings.workflow == "spatial":
        if cancel.wait(8):
            raise InterruptedError("Worker cancelled")
        raise TimeoutError("Cancel never arrived")
    target = directory / "fixture.mp4"
    target.write_bytes(Path(source).read_bytes())
    return {"status":"success", "output_path":str(target), "workflow":settings.workflow}
""", encoding="utf-8")
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"fixture video bytes")
    games = []
    q = Queue(tmp_path / "spool", guard=lambda: games)
    payload = {"installation":str(backend),"input":str(src)}
    manual = q.enqueue("finish",{**payload,"settings":{"workflow":"spatial"}})["id"]
    auto = q.enqueue("finish",payload)["id"]
    director = q.enqueue("director",{"url":"http://127.0.0.1:1","model":"chosen","prompt":"scene"})["id"]
    q.tick()
    q = Queue(q.root,guard=lambda: games)
    progress = poll(q, manual, lambda job: job["receipt"].get("progress") == 0.5)
    assert progress["status"] == "working" and q.get(auto)["status"] == "queued"
    q.cancel(director)
    games.append("RocketLeague.exe")
    assert poll(q, manual, lambda j: j["status"] in TERMINAL)["status"] == "cancelled"
    q.tick()
    assert q.get(auto)["status"] == "queued"
    games.clear()
    finished = poll(q, auto, lambda j: j["status"] in TERMINAL)
    assert finished["status"] == "succeeded", finished["message"]
    result = finished["receipt"]["result"]
    assert result["workflow"] == "auto" and result["sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()
    assert Path(result["output_path"]).is_relative_to(q.root / auto / "outputs")
    assert not (q.root / director / "claim").exists()


def test_director_selected_model_and_stream_identity():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"data":[{"id":"selected-qwen"}]}')

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.server.hold:
                self.server.entered.set()
                self.server.release.wait(3)
                return
            self.send_response(200)
            self.end_headers()
            event = {"model":self.server.identity, "choices":[{"delta":{"content":"A lighthouse at dawn"}}]}
            self.server.requests.append(data)
            self.wfile.write(("data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n").encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.identity = "selected-qwen"
    server.requests = []
    server.hold = False
    server.entered = threading.Event()
    server.release = threading.Event()
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    payload = {"url":f"http://127.0.0.1:{server.server_port}","model":"selected-qwen","prompt":"Opening"}
    try:
        film = direct({**payload,"mode":"film"}, threading.Event())
        game = direct({**payload,"mode":"game"}, threading.Event())
        assert film["storyboard"] == game["storyboard"] == "A lighthouse at dawn"
        assert server.requests[0]["messages"][0] != server.requests[1]["messages"][0]
        with pytest.raises(ValueError, match="unavailable"):
            direct({**payload,"model":"missing-preset"}, threading.Event())
        assert len(server.requests) == 2
        server.identity = "wrong"
        with pytest.raises(ValueError, match="different model"):
            direct(payload, threading.Event())
        server.identity = None
        with pytest.raises(ValueError, match="did not identify"):
            direct(payload, threading.Event())
        server.hold = True
        cancel = threading.Event()
        def game_begins():
            if server.entered.wait(2):
                cancel.set()
        guard = threading.Thread(target=game_begins)
        guard.start()
        started = time.monotonic()
        with pytest.raises(InterruptedError):
            direct(payload, cancel)
        guard.join()
        assert time.monotonic() - started < 2
    finally:
        server.release.set()
        server.shutdown()
        thread.join()
        server.server_close()
