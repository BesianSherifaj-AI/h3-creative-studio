import io
import json
import os
import importlib.util
import shutil
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from h3studio import voice
from h3studio.project import Project


def pcm(frames=800, channels=1):
    sink = io.BytesIO()
    with wave.open(sink, "wb") as out:
        out.setparams((channels, 2, 8000, 0, "NONE", ""))
        out.writeframes(b"\x00\x12" * channels * frames)
    return sink.getvalue()


@pytest.fixture()
def speech_server():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/v1/models"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"data":[{"id":"local-voice","voices":["harbor"]}]}')

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, data))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(pcm(800 if data["input"].startswith("Follow") else 1600))

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_server_narration_timing_mixing_and_portable_reopen(tmp_path, speech_server):
    url, received = speech_server
    project = Project.create(tmp_path / "Original", "Lantern")
    assert voice.action(None, project, "voice_models", {"endpoint":url}) == {
        "models":[{"id":"local-voice","voices":["harbor"]}]}
    with project.transaction() as doc:
        doc["director"] = {"turns": {"night-watch": {}}}
    args = dict(engine="local", endpoint=url, model="local-voice", voice="harbor", rate=1.25,
                speaker="Mara", turn="night-watch", text="Follow the lantern. The tide is rising.",
                pronunciations={"lantern":"lan-tern"})
    clip = voice.render(project, args)
    assert [path for path, data in received] == ["/v1/audio/speech"] * 2
    assert received[0][1] == {"model": "local-voice", "voice": "harbor", "speed": 1.25,
                               "input": "Follow the lan-tern.", "response_format": "wav"}
    assert clip["source"] == {"turn": "night-watch"}
    assert [(c["start"], c["end"]) for c in clip["cues"]] == [(0, 800), (1600, 3200)]
    assert voice.caption_rows(project.state()) == []
    assert "00:00:00,200 --> 00:00:00,400" in voice.captions(project.state(), clip["id"])
    with wave.open(str(project.asset_path(clip["asset"])), "rb") as src:
        assert src.getnframes() == 3200 and src.getframerate() == 8000
        src.setpos(800)
        assert src.readframes(800) == bytes(1600)
    mix = dict(id=clip["id"], offset=2.125, gain_db=-4, track=1)
    bed = voice.place(project, mix)
    assert voice.place(project, mix) == bed
    assert len(project.state()["soundtracks"]) == 1
    shutil.move(str(project.folder), tmp_path / "Moved")
    project = Project(tmp_path / "Moved")
    assert project.asset_path(clip["asset"]).is_file()
    assert project.state()["soundtracks"][bed["id"]]["asset"] == clip["asset"]
    result = voice.action(None, project, "voice_captions", {"path": str(tmp_path / "Film.srt")})
    assert Path(result["path"]).read_text(encoding="utf-8") == (
        "1\n00:00:02,125 --> 00:00:02,225\nMara: Follow the lantern.\n\n"
        "2\n00:00:02,325 --> 00:00:02,525\nMara: The tide is rising.\n")
    with pytest.raises(ValueError, match="preserve"):
        voice.action(None, project, "voice_captions", result)
    for update in ({"gain_db": "nan"}, {"offset": -1}, {"track": 8}):
        with pytest.raises(ValueError):
            voice.place(project, {**mix, **update})
        assert project.state()["soundtracks"][bed["id"]] == bed


def test_reject_remote_invalid_voice_inputs(tmp_path):
    project = Project.create(tmp_path, "Voice test")
    args = dict(engine="local", endpoint="http://127.0.0.1:8880", model="local", voice="one", text="First.")
    for change in ({"endpoint": "https://example.com:8080"}, {"endpoint": "http://127.0.0.1:80/other"},
                   {"endpoint": "http://me:secret@localhost:8000"}, {"text": "   "}, {"rate": float("nan")},
                   {"voice": ""}, {"shot": "missing"}, {"turn": "missing"}, {"engine": "unknown"}):
        with pytest.raises(ValueError):
            voice.render(project, {**args, **change})
    with pytest.raises(InterruptedError):
        voice.render(project, args, cancelled=lambda: True)
    assert "narrations" not in project.state()


def test_bad_audio_has_no_project_side_effect(tmp_path, monkeypatch):
    project = Project.create(tmp_path, "Empty response")

    def broken(folder, spec, segments, cancel, report):
        (folder / "0.wav").write_bytes(b"{}")

    monkeypatch.setattr(voice, "_server_audio", broken)
    with pytest.raises(ValueError, match="PCM WAV"):
        voice.render(project, dict(engine="local", endpoint="http://127.0.0.1:1234", model="mock",
                                   voice="alpha", text="This response is malformed."))
    assert not project.state()["assets"] and "narrations" not in project.state()


def test_pronunciation_changes_spoken_names_and_preserves_the_authored_captions(tmp_path, monkeypatch):
    project = Project.create(tmp_path, "Tidal Relay")

    def synthesis(folder, spec, spoken, cancel, report):
        assert spoken == ["Open Tide-al Relay, not Tidally.", "Tide-al welcomes you."]
        for idx in range(2):
            (folder / f"{idx}.wav").write_bytes(pcm())

    monkeypatch.setattr(voice, "_server_audio", synthesis)
    spec = dict(engine="local",endpoint="http://localhost:8880",model="model",voice="one",
                text="Open Tidal Relay, not Tidally. Tidal welcomes you.",pronunciations={"Tidal":"Tide-al"})
    clip = voice.render(project, spec)
    assert clip["pronunciations"] == {"Tidal":"Tide-al"} and clip["text"] == spec["text"]
    assert "Tidal Relay" in voice.captions(project.state(), clip["id"])
    assert "Tide-al" not in voice.captions(project.state(), clip["id"])
    with pytest.raises(ValueError):
        voice.render(project, {**spec, "pronunciations":{"Tidal":"a\nb"}})


def test_cancel_while_waiting_for_first_speech_byte(tmp_path):
    accepted, release = threading.Event(), threading.Event()

    class Delayed(BaseHTTPRequestHandler):
        def do_POST(self):
            accepted.set()
            release.wait(5)
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    project = Project.create(tmp_path, "Cancelled")
    with ThreadingHTTPServer(("127.0.0.1", 0), Delayed) as worker:
        thread = threading.Thread(target=worker.serve_forever, daemon=True)
        thread.start()
        try:
            with pytest.raises(InterruptedError):
                voice.render(project, dict(engine="local", endpoint=f"http://127.0.0.1:{worker.server_port}",
                             model="local", voice="sample", text="A pending request."), cancelled=accepted.is_set)
            assert accepted.is_set() and not project.state()["assets"]
        finally:
            release.set()
            worker.shutdown()
            thread.join()


def test_optional_adapter_uses_exact_model_voice_and_wav():
    path = Path(__file__).resolve().parents[1] / "scripts/local_speech.py"
    spec = importlib.util.spec_from_file_location("h3_test_local_speech", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Sound:
        model_id = module.KOKORO
        sample_rate = 24000
        voice_presets = {"af_heart"}

        @staticmethod
        def pcm(text, name, speed):
            assert (text, name, speed) == ("Hello.", "af_heart", 1.25)
            return bytes(1000)

    args = dict(model=module.KOKORO, voice="af_heart", speed=1.25, input="Hello.")
    with wave.open(io.BytesIO(module.synthesize(Sound(), args))) as result:
        assert result.getframerate() == 24000 and result.getnframes() == 500
    for change in ({"model":"unknown"}, {"voice":"other"}, {"speed":4}, {"response_format":"mp3"}):
        with pytest.raises(ValueError):
            module.synthesize(Sound(), {**args, **change})


@pytest.mark.skipif(os.name != "nt", reason="Windows installed voices")
def test_installed_windows_speech_is_audible_pcm(tmp_path):
    installed = voice.voices()
    if not installed:
        pytest.skip("No installed System.Speech desktop voice")
    project = Project.create(tmp_path, "Offline")
    clip = voice.render(project, dict(engine="windows", voice=installed[0]["name"],
                                      text="Follow the lantern. Welcome home."))
    assert len(clip["cues"]) == 2 and 0.8 < clip["duration"] < 25
    with wave.open(str(project.asset_path(clip["asset"])), "rb") as src:
        import numpy as np
        signal = np.frombuffer(src.readframes(src.getnframes()), dtype="<i2").astype(float)
        assert np.sqrt(np.mean(signal ** 2)) > 100
        assert np.abs(signal).max() < 32768
