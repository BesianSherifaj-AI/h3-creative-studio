"""Director boards and Game branches survive reopen, partial approvals and stale requests."""
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from h3studio import generation
from h3studio.integrations import engine_action
from h3studio.jobs import Queue
from h3studio.project import Project
from test_generation import setup_case


@pytest.fixture
def setup(tmp_path,monkeypatch):
    p = Project.create(tmp_path/"film","Director")
    studio = SimpleNamespace(queue=Queue(tmp_path/"jobs",guard=lambda:[]),
        prefs={"video_url":"http://127.0.0.1:8021","director_url":"http://127.0.0.1:1234","director_model":"selected-qwen"})
    _, fake, _ = setup_case(tmp_path)
    prepare, validate = generation.prepare_take, generation.validate_saved_take
    monkeypatch.setattr(generation,"prepare_take",lambda spec,resolve:prepare(spec,resolve,client=fake))
    monkeypatch.setattr(generation,"validate_saved_take",lambda bundle:validate(bundle,client=fake))
    def call(op,**a):
        return engine_action(studio,Project(p.folder),op,a)
    def reply(job,obj):
        pl=studio.queue.get(job["id"],full=True)["payload"]
        result={"mode":pl["mode"],"model":pl["model"],"storyboard":obj if isinstance(obj,str) else json.dumps(obj)}
        studio.queue._update(job["id"],"succeeded","Director",{"result":result})
        return call("adopt",id=job["id"])["director_review"]
    return p,studio,call,reply


def film(title="First",duration=4):
    return {"title":"North crossing","shots":[{"title":title,"duration":duration,"prompt":"Waves reach the beacon."},
                                           {"title":"Window","duration":5,"prompt":"A hand opens the window."}]}


def test_two_shot_review_lock_rerender_and_reopen(setup):
    p,studio,call,reply=setup
    board=reply(call("director",mode="film",prompt="An offshore crossing",count=2),film())
    assert p.state()["cut"] == []
    call("director_approve",review=board,row=1)
    call("director_approve",review=board)
    call("director_approve",review=board)
    s1,s2=p.state()["cut"]
    rows=p.state()["director"]["boards"][board]["rows"]
    assert [r["shot"] for r in rows]==[s1,s2]
    assert p.state()["shots"][s1]["director_prompt"]==film()["shots"][0]["prompt"]
    with p.transaction() as doc:
        doc["recipe_default"] = {"seed":19,"mode":"t2va","settings":{"resolution":"0.3","save_mmh3":False}}
    ticket = str(uuid.uuid4())
    jobs = call("director_render",board=board,request_id=ticket)["jobs"]
    assert len(jobs)==2
    request = p.state()["prepared"][jobs[0].replace("-","")]["request"]
    assert request["seed"] == 19 and request["settings"]["save_mmh3"] is False
    assert call("director_render",board=board,request_id=ticket)["jobs"]==jobs
    assert call("director_render",board=board,request_id=str(uuid.uuid4()))["jobs"]==[]
    def finish(j,n):
        movie=p.folder.parent/("fixture-"+str(n)+".mp4")
        movie.write_bytes(("fixture "+str(n)).encode())
        import hashlib
        output={"node_id":"1","path":str(movie),"sha256":hashlib.sha256(movie.read_bytes()).hexdigest()}
        studio.queue._update(j,"succeeded","Video",{"outputs":[output]})
        return call("adopt",id=j)["take"]
    left,right=(finish(j,i) for i,j in enumerate(jobs))
    assert [p.state()["shots"][s]["selected_take"] for s in (s1,s2)]==[left,right]
    call("director_lock",board=board,row=0,take=left,locked=True)
    with pytest.raises(ValueError,match="Unlock this shot"):
        p.branch_take(left,"retry")
    assert Project(p.folder).state()["cut"] == [s1,s2]
    prior=jobs[1].replace("-","")
    again=call("director_render",board=board,row=1,request_id=str(uuid.uuid4()),retry=True,previous=prior)["jobs"][0]
    assert p.state()["prepared"][again.replace("-","")]["request"]["seed"] != 19
    updated=finish(again,3)
    assert right!=updated and p.state()["shots"][s2]["selected_take"]==updated
    assert p.state()["shots"][s1]["selected_take"]==left
    late=p.save_take(s1,{"kind":"completed earlier transaction"})
    assert p.state()["shots"][s1]["selected_take"]==left
    with pytest.raises(ValueError,match="Unlock"):
        p.select_take(s1,late)
    with pytest.raises(ValueError,match="unlock"):
        call("director_render",board=board,row=0,request_id=str(uuid.uuid4()))
    with pytest.raises(ValueError,match="newer render"):
        call("director_render",board=board,row=1,request_id=str(uuid.uuid4()),retry=True,previous=prior)
    revised=film("Interior",5)
    revised["shots"]=revised["shots"][:1]
    revision=reply(call("director",board=board,row=1,mode="film",prompt="Focus on the lantern."),revised)
    call("director_approve",review=revision)
    assert p.state()["shots"][s2]["name"]=="Interior"
    assert p.state()["takes"][updated]["settings"]["request"]["prompt"]=="A hand opens the window."
    assert p.state()["shots"][s1]["selected_take"]==left
    call("director_lock",board=board,row=0,take=left,locked=False)
    retry = p.branch_take(left,"retry")
    assert Project(p.folder).state()["cut"] == [retry,s2]


def test_forked_game_request_uses_accepted_path_only(setup):
    p,studio,call,reply=setup
    def turn(parent,action,scene):
        job=call("director",mode="game",prompt=action,parent=parent)
        rid=reply(job,{"scene":scene,"actions":["Wait","Go"],"state_changes":["Tide changed"]})
        assert rid not in p.state()["director"]["turns"]
        call("director_approve",review=rid)
        return rid
    root=turn(None,"Reach the quay","The key glints.")
    first=turn(root,"Take the key","The key is held.")
    second=turn(root,"Walk away","Mira walks inland.")
    call("director_resume",turn=first)
    assert p.state()["director"]["active_turn"]==first
    child=call("director",mode="game",prompt="Wait",parent=first)
    payload=studio.queue.get(child["id"],full=True)["payload"]
    assert "The key glints." in payload["prompt"] and "The key is held." in payload["prompt"]
    assert "Mira walks inland." not in payload["prompt"]
    assert p.state()["director"]["turns"][second]["parent"]==root
    call("director_approve",review=second)
    assert p.state()["director"]["active_turn"]==first


def test_structured_correction_and_legacy_receipt(setup):
    p,studio,call,reply=setup
    bad=reply(call("director",mode="film",prompt="Two shots",count=2),"Invalid storyboard")
    assert p.state()["director"]["reviews"][bad]["error"]
    with pytest.raises(ValueError):
        call("director_approve",review=bad,correction=json.dumps(film(duration=2)))
    call("director_approve",review=bad,correction=json.dumps(film()))
    legacy=studio.queue.enqueue("director",{"url":studio.prefs["director_url"],"studio_project":p.state()["id"],
            "mode":"film","model":"selected-qwen","prompt":"A short film"})
    rid=reply(legacy,"Lantern in fog.")
    call("director_approve",review=rid)
    assert p.state()["director"]["boards"][rid]["rows"][0]["prompt"]=="Lantern in fog."
