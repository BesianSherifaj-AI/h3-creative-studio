"""Portable selected cuts, approval, lineage, audio phase fallback and master adoption."""
import hashlib
from types import SimpleNamespace

import httpx
import pytest

from h3studio.integrations import engine_action
from h3studio.jobs import Queue
from h3studio.project import Project
from test_media_join import decoded_schema, mixed_records, records, schema
from test_project import motion_packet


def create_cut(tmp_path, monkeypatch, entries):
    project = Project.create(tmp_path / "film", "Packet masters")
    cards = {}
    parent = None
    previous_asset = None
    takes = []
    for index, record in enumerate(entries, 1):
        tr = record["transfer"]
        seed = 100 + index
        selector = "output::" + record["run"]["continuation_source"]
        revision = "version-" + str(index)
        frames = tr["manifest"]["frames"]
        packet = motion_packet(tmp_path / f"clip-{index}.mmh3", frames=frames, seed=seed)
        asset = project.add_asset(packet)
        start = 39 if parent else 0
        end = frames - 4
        shot = project.add_shot(f"Part {index}", (end - start) / 24)
        settings = {**tr["manifest"], "project_id":project.state()["id"],"width":128,"height":64,
                    "graph":tr["prompt"],"motion_selector":selector,"motion_revision":revision,
                    "packet_id":"packet-1","seed":seed,"delivery_trim":[start/24,end/24]}
        if parent:
            settings["parent"] = {"take_id":parent,"sha256":previous_asset}
            settings["mmh3"]["source_packet_id"] = "packet-1"
        parent = project.save_take(shot,settings,packet_asset=asset,parent_take=parent)
        previous_asset = asset
        takes.append(parent)
        cards[selector] = {"id":"packet-1","revision":revision,"has":{"latent":True}}

    def respond(request):
        if request.url.path == "/object_info":
            return httpx.Response(200,json={**decoded_schema(schema()),"LoadImage":{}})
        if request.url.path == "/mmh3_media/file_info":
            return httpx.Response(200,json=cards[request.url.params["file"]])
        raise AssertionError(request.url)

    create_client = httpx.Client
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(httpx,"Client",lambda **kwargs:create_client(**kwargs,transport=transport))
    studio = SimpleNamespace(prefs={"video_url":"http://127.0.0.1:8021"},queue=Queue(tmp_path/"queue",guard=lambda:[]))
    return project,studio,takes,cards


def test_review_reopen_dispatch_and_archive_separate_master(tmp_path,monkeypatch):
    p,studio,takes,cards = create_cut(tmp_path,monkeypatch,records())
    result = engine_action(studio,p,"prepare_master",{})
    assert studio.queue.list() == []
    m = result["manifest"]
    assert (m["assembly_mode"], m["frames"], m["editorial_frames"]) == ("latent",209,201)
    assert [s["take"] for s in m["segments"]] == takes
    reopened = Project(p.folder)
    args = {"plan":result["plan"]}
    job = engine_action(studio,reopened,"submit_master",args)
    assert engine_action(studio,reopened,"submit_master",args)["id"] == job["id"]
    original_cut = p.state()["cut"]
    movie = tmp_path/"master.mp4"
    movie.write_bytes(b"mock-renderer-savevideo-output")
    digest = hashlib.sha256(movie.read_bytes()).hexdigest()
    output = {"node_id":m["video_save_node_id"],"filename":movie.name,"path":str(movie),"sha256":digest}
    studio.queue._update(job["id"],"succeeded","Complete",{"outputs":[output]})
    saved = engine_action(studio,p,"adopt",{"id":job["id"]})
    assert saved == {"master":job["id"], "asset":digest}
    assert engine_action(studio,reopened,"adopt",{"id":job["id"]}) == saved
    assert reopened.state()["cut"] == original_cut
    assert [reopened.state()["shots"][s]["selected_take"] for s in original_cut] == takes
    dest = tmp_path/"exported.mp4"
    engine_action(studio,reopened,"export_master",{"master":saved["master"],"path":str(dest)})
    assert dest.read_bytes() == movie.read_bytes()
    next(iter(cards.values()))["revision"] = "changed-after-review"
    with pytest.raises(ValueError,match="changed on the renderer"):
        engine_action(studio,reopened,"submit_master",args)


def test_phase_shortfall_and_wrong_branch(tmp_path,monkeypatch):
    p,studio,takes,_ = create_cut(tmp_path,monkeypatch,mixed_records())
    result = engine_action(studio,p,"prepare_master",{"takes":takes})
    assert result["manifest"]["assembly_mode"] == "streaming_decoded"
    assert result["manifest"]["audio_boundary_preflight"]["boundaries"][2]["shortfall_ticks"] == 1
    with pytest.raises(ValueError,match="exact saved parent"):
        engine_action(studio,p,"prepare_master",{"takes":[takes[0],takes[2]]})
    with p.transaction() as data:
        graph = data["takes"][takes[1]]["settings"]["graph"]
        graph["7"]["inputs"]["file"] = "output::mmh3/a-different-branch.mmh3"
    with pytest.raises(ValueError,match="loads a different parent"):
        engine_action(studio,p,"prepare_master",{"takes":takes[:2]})
