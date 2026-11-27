"""Desktop engine actions through real project/queue storage with synthetic Comfy I/O."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from h3studio import generation
from h3studio.integrations import engine_action
from h3studio.jobs import Queue
from h3studio.project import Project
from test_generation import setup_case
from test_project import motion_packet


@pytest.fixture
def setup_studio(tmp_path, monkeypatch):
    p = Project.create(tmp_path / "portable", "Film")
    path = tmp_path / "Guide.png"
    Image.new("RGB", (64, 48), "orange").save(path)
    asset = p.add_asset(path)
    rev = p.save_composition("Layered still", 64, 48, [{"name": "Background", "asset": asset}])
    shot = p.add_shot("Shot one", 5)
    p.set_guides(shot, [{"frame": f, "role": n, "revision": rev} for f,n in [(0,"first"),(60,"middle"),(119,"last")]])
    spec, fake, _ = setup_case(tmp_path)
    def resolve(project, revision):
        dest = tmp_path / (revision + ".png")
        project.render_revision(revision).save(dest)
        return dest
    studio = SimpleNamespace(prefs={"video_url":spec["comfy_url"]}, queue=Queue(tmp_path / "spool", guard=lambda: []), _revision=resolve)
    prepare, validate = generation.prepare_take, generation.validate_saved_take
    monkeypatch.setattr(generation, "prepare_take", lambda spec, resolve: prepare(spec, resolve, client=fake))
    monkeypatch.setattr(generation, "validate_saved_take", lambda bundle: validate(bundle, client=fake))
    return studio, p, shot, rev, fake


def test_desktop_prepare_reopen_submit_and_revalidate(setup_studio):
    studio, p, shot, rev, fake = setup_studio
    a = {"mode":"fl2va","shot":shot,"seed":12,"prompt":"Light on a moving river."}
    plan = engine_action(studio, p, "prepare", a)
    assert not studio.queue.list()
    m = plan["manifest"]
    assert [s["role"] for s in m["references"]] == ["first_frame", "last_frame"]
    assert [s["revision"] for s in m["guides"]] == [rev, rev]
    assert {s["frame_idx"] for s in m["guides"]} == {60,119}
    reopened = Project(p.folder)
    submitted = engine_action(studio, reopened, "submit_prepared", {"plan":plan["plan"]})
    assert submitted["status"] == "queued"
    assert len(studio.queue.list()) == 1
    assert engine_action(studio, reopened, "submit_prepared", {"plan":plan["plan"]})["id"] == submitted["id"]
    fake.schema["LoraLoaderModelOnly"]["input"]["required"]["lora_name"] = [["missing.safetensors"]]
    with pytest.raises(ValueError, match="lora_name"):
        engine_action(studio, reopened, "submit_prepared", {"plan":plan["plan"]})


def test_motion_and_visual_branch_use_exact_displayed_take(tmp_path, setup_studio):
    studio, p, shot, rev, fake = setup_studio
    archive = motion_packet(tmp_path / "shot.mmh3")
    packet_id = p.add_asset(archive)
    fake.card.update(id="packet-1", geometry={"width":128,"height":64,"frames":124,"fps":24})
    settings = {"motion_revision":"saved-revision", "motion_selector":"output::mmh3/source.mmh3",
                "render_trim":[0,120],"delivery_trim":[0,5.0],"seed":123}
    take = p.save_take(shot, settings, packet_asset=packet_id)
    child = p.branch_take(take, "continue")
    p.choose_start(child, "original_motion")
    p.set_guides(child, [{"role":"last","frame":119,"revision":rev}])
    a = {"mode":"t2va","shot":child,"seed":22,"prompt":"The water rises."}
    plan = engine_action(studio, Project(p.folder), "prepare", a)
    assert plan["manifest"]["parent"]["sha256"] == packet_id
    assert plan["manifest"]["parent"]["latent_revision"] == 2
    assert plan["manifest"]["guides"][-1]["frame_idx"] == 158
    engine_action(studio, p, "submit_prepared", {"plan":plan["plan"]})
    p.choose_start(child, "visual", rev)
    plan = engine_action(studio, p, "prepare", a)
    assert plan["manifest"]["parent"] is None
    assert plan["manifest"]["guides"][0]["revision"] == rev
    assert plan["manifest"]["guides"][0]["frame_idx"] == 0
    p.choose_start(child, "regenerate", rev)
    plan = engine_action(studio, p, "prepare", a)
    m = plan["manifest"]
    assert m["timeline"]["trim"] == [4,124] and m["mmh3"]["save_enabled"]
    assert m["guides"][0]["frame_idx"] == 4 and m["guides"][-1]["frame_idx"] == 123


def test_adopt_receipt_packet_and_idempotent_retry(tmp_path, setup_studio):
    studio,p,shot,rev,_ = setup_studio
    video = tmp_path / "synthetic.mp4"
    video.write_bytes(b"fixture for archive adoption")
    archive = motion_packet(tmp_path / "shot.mmh3")
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    m = {"seed":123,"width":128,"height":64,"frames":124,"timeline":generation.timing(5)}
    payload = {"comfy_url":studio.prefs["video_url"],"prompt":{"1":{"class_type":"SaveVideo","inputs":{}}},
               "studio_project":p.state()["id"],"studio_shot":shot,"manifest":m,"studio_guides":[{"frame":60,"revision":rev}]}
    job = studio.queue.enqueue("video",payload)["id"]
    receipt = {"outputs":[{"path":str(video),"sha256":hashlib.sha256(video.read_bytes()).hexdigest()}],"packet":{"path":str(archive),"sha256":sha,"packet_id":"packet-1",
               "packet_revision":"renderer-token","selector":"output::mmh3/test.mmh3"}}
    studio.queue._update(job,"succeeded","Fixture render",receipt)
    original = video.read_bytes()
    video.write_bytes(b"replaced after submission")
    with pytest.raises(ValueError, match="checksum"):
        engine_action(studio, p, "adopt", {"id":job})
    video.write_bytes(original)
    result = engine_action(studio, p, "adopt", {"id":job})
    assert result == engine_action(studio, Project(p.folder), "adopt", {"id":job})
    adopted = p.state()["takes"][result["take"]]
    assert adopted["packet_asset"] == sha and adopted["settings"]["motion_revision"] == "renderer-token"
    assert adopted["packet"]["revision"] == 2 and adopted["settings"]["render_trim"] == [0,120]


def test_moved_packet_uses_new_renderer_timestamp_token(tmp_path):
    path = motion_packet(tmp_path / "moved.mmh3")
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    parent = {"packet_id":"packet-1","packet_revision":"old-mtime-token","sha256":sha}
    def handle(req):
        if req.url.path == "/upload/image":
            return httpx.Response(200, json={"subfolder":"h3_creative_studio/restored/"+sha[:16],"type":"output","name":sha+".mmh3"})
        if req.url.path == "/view":
            return httpx.Response(200, content=data)
        if req.url.path == "/mmh3_media/file_info":
            return httpx.Response(200, json={"id":"packet-1","revision":"new-mtime-token","has":{"latent":True}})
        raise AssertionError(req.url)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        selector, token = generation.restore_packet("http://127.0.0.1:8010", path, parent, client)
    assert token == "new-mtime-token" and selector.endswith(sha+".mmh3")
