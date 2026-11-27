from concurrent.futures import ThreadPoolExecutor
import subprocess

import pytest

from h3studio import sample
from h3studio.project import Project


def test_first_open_is_atomic_and_requires_no_runtime(tmp_path,monkeypatch):
    def no_process(*args,**kw):
        raise AssertionError("Opening the sample started an external process.")
    monkeypatch.setattr(subprocess,"Popen",no_process)
    with ThreadPoolExecutor(max_workers=2) as pool:
        copies=list(pool.map(sample.open_example,[tmp_path,tmp_path]))
    a,b=copies
    assert a.folder==b.folder and a.state()["id"]==b.state()["id"]
    state=a.state()
    assert state["name"]=="Tidal Relay" and len(state["cut"])==3
    assert len(state["compositions"])==3 and len(state["revisions"])==9
    shots=[state["shots"][j] for j in state["cut"]]
    assert state["cut_history"]==[[{"shot":s["id"],"take":s["selected_take"]} for s in shots]]
    assert [s["duration"] for s in shots]==[4,3,3]
    assert [s["recipe"]["mode"] for s in shots]==["fl2va","i2va","l2va"]
    assert len({s["recipe"]["prompt"] for s in shots})==3
    for shot in shots:
        frames=shot["duration"]*24
        assert [g["frame"] for g in shot["guides"]]==[0,frames//2,frames-1]
        revisions=[state["revisions"][g["revision"]] for g in shot["guides"]]
        assert all(len(r["layers"])>=2 and (r["width"],r["height"])==(1600,900) for r in revisions)
        assert revisions[0]["layers"][0]!=revisions[-1]["layers"][0]
        image=a.render_revision(revisions[0]["id"])
        assert image.size==(1600,900) and image.getchannel("A").getextrema()==(255,255)
        take=state["takes"][shot["selected_take"]]
        assert take["settings"]["kind"]=="Studio animatic" and take["settings"]["audio_route"]==[None]
        assert take["guides"]==shot["guides"] and a.asset_path(take["video_asset"]).suffix==".mp4"
        assert not take["packet"] and len(set(g["revision"] for g in shot["guides"]))==3
    assert len(state["director"]["boards"])==len(state["director"]["turns"])==1
    score=next(iter(state["soundtracks"].values()))
    assert score["name"]=="Listening Line" and a.asset_path(score["asset"]).suffix==".m4a"
    with a.transaction() as doc:
        doc["shots"][shots[1]["id"]]["name"]="My new scene name"
    assert sample.open_example(tmp_path).state()["shots"][shots[1]["id"]]["name"]=="My new scene name"


def test_failed_first_open_leaves_no_partial_project(tmp_path,monkeypatch):
    original=sample._build
    def fail(staged):
        staged.add_shot("Only partly built")
        raise OSError("Interrupted import")
    monkeypatch.setattr(sample,"_build",fail)
    with pytest.raises(OSError,match="Interrupted"):
        sample.open_example(tmp_path)
    assert not (tmp_path/"Examples"/"Tidal Relay").exists()
    monkeypatch.setattr(sample,"_build",original)
    project=sample.open_example(tmp_path)
    assert len(Project(project.folder).state()["cut"])==3
