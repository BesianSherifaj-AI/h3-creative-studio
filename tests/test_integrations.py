"""Desktop engine actions through real project/queue storage with synthetic Comfy I/O."""
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from h3studio import generation
from h3studio.integrations import engine_action
from h3studio.jobs import Queue
from h3studio.project import Project
from test_generation import node_schema, setup_case
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
    original = p.state()["prepared"][plan["plan"]]
    continued = p.save_take(child,{**original["bundle"]["manifest"],"request":original["request"]},guides=original["guides"])
    p.choose_start(child, "visual", rev)
    rerun = engine_action(studio,Project(p.folder),"prepare",{"shot":child,"source_take":continued})
    assert rerun["manifest"]["parent"] == plan["manifest"]["parent"]
    assert rerun["manifest"]["guides"][-1]["frame_idx"] == 158
    engine_action(studio,p,"submit_prepared",{"plan":rerun["plan"]})
    assert p.state()["shots"][child]["start_mode"] == "visual"
    plan = engine_action(studio, p, "prepare", a)
    assert plan["manifest"]["parent"] is None
    assert plan["manifest"]["guides"][0]["revision"] == rev
    assert plan["manifest"]["guides"][0]["frame_idx"] == 0
    p.choose_start(child, "regenerate", rev)
    plan = engine_action(studio, p, "prepare", a)
    m = plan["manifest"]
    assert m["timeline"]["trim"] == [4,124] and m["mmh3"]["save_enabled"]
    assert m["guides"][0]["frame_idx"] == 4 and m["guides"][-1]["frame_idx"] == 123


def test_selected_recipes_roundtrip_with_independent_draft(tmp_path,setup_studio):
    from vendor.prompt.comfy_transfer import FL_LORA
    studio,p,shot,first_revision,fake = setup_studio
    extra = "Harbor.safetensors"
    fake.schema["LoraLoaderModelOnly"]["input"]["required"]["lora_name"][0].append(extra)
    first_stack = [{"name":FL_LORA,"strength":0.8},{"name":extra,"strength":0.3}]
    second_stack = list(reversed(first_stack))
    guides = lambda revision:[{"frame":60,"role":"middle","revision":revision,"usage":"timed"}]
    def call(op,**args):
        return engine_action(studio,Project(p.folder),op,{"shot":shot,**args})
    def old_take(prompt,seed,stack,revision,legacy):
        selected = guides(revision)
        p.set_guides(shot,selected)
        request = {"mode":"t2va","prompt":prompt,"seed":seed,"settings":{"loras_by_mode":{"t2va":stack}}}
        plan = call("prepare",**request)
        with p.transaction() as doc:
            row = doc["prepared"].pop(plan["plan"])
        settings = {**plan["manifest"],"graph":row["bundle"]["prompt"]}
        if not legacy:
            settings["request"] = request
        return p.save_take(shot,settings,guides=[] if legacy else selected)
    a = old_take("A buoy glows.",101,first_stack,first_revision,True)
    second_revision = p.save_composition("Different middle",64,48,[])
    b = old_take("A boat arrives.",202,second_stack,second_revision,False)
    live_revision = p.save_composition("Independent draft",64,48,[])
    live_guides = guides(live_revision)
    p.set_guides(shot,live_guides)
    draft = {"prompt":"Explore a different route.","seed":303,"mode":"t2va","settings":{}}
    call("recipe_save",recipe=draft)
    form = {**draft,"prompt":"Keep my unfinished draft."}
    call("recipe_draft",recipe=form)
    for i,(ident,prompt,seed,stack,rev) in enumerate(((a,"A buoy glows.",101,first_stack,first_revision),
        (b,"A boat arrives.",202,second_stack,second_revision),(a,"A buoy glows.",101,first_stack,first_revision))):
        Project(p.folder).select_take(shot,ident)
        recovered = call("recipe_get")
        frozen = recovered["selected"]
        assert recovered["recipe"]==form and frozen["id"]==ident
        assert (frozen["recipe"]["prompt"],frozen["recipe"]["seed"])==(prompt,seed)
        assert frozen["recipe"]["settings"]["loras_by_mode"]["t2va"]==stack
        assert frozen["guides"]==guides(rev)
        # A canceled review, a take-specific override, then an exact rerun.
        override = {"seed":404} if i==1 else {}
        plan = call("prepare",source_take=ident,**override)
        manifest = plan["manifest"]
        assert manifest["prompt_sha256"]==hashlib.sha256(prompt.encode()).hexdigest()
        assert (manifest["seed"],manifest["recipe_source_take"])==(override.get("seed",seed),ident)
        assert [l["name"] for l in manifest["loras"]]==[l["name"] for l in stack]
        assert manifest["guides"][0]["revision"]==rev and manifest["guides"][0]["frame"]==60
        if i:
            job = call("submit_prepared",plan=plan["plan"])["id"]
            assert studio.queue.get(job,full=True)["payload"]["studio_guides"]==guides(rev)
            clip = tmp_path/"take.mp4"
            clip.write_bytes(b"synthetic receipt fixture")
            studio.queue._update(job,"succeeded","Done",{"outputs":[{"path":str(clip),"sha256":hashlib.sha256(clip.read_bytes()).hexdigest()}]})
            new = call("adopt",id=job)["take"]
            stored = p.state()["takes"][new]
            assert stored["guides"]==guides(rev) and stored["settings"]["recipe_source_take"]==ident
            assert stored["settings"]["request"]["seed"]==manifest["seed"]
        assert p.state()["shots"][shot]["recipe"]==draft and p.state()["shots"][shot]["guides"]==live_guides
        assert call("recipe_get")["recipe"]==form
    retry = p.branch_take(b,"retry")
    assert engine_action(studio,Project(p.folder),"recipe_get",{"shot":retry})["selected"]["id"]==b
    with pytest.raises(ValueError,match="belonging"):
        engine_action(studio,p,"prepare",{"shot":retry,"source_take":a})
    with p.transaction() as doc:
        doc["takes"][a]["settings"]["guides"][0]["revision"]="unavailable-revision"
    p.select_take(shot,a)
    assert "composition revision" in call("recipe_get")["unavailable"]


def test_continuation_review_commits_only_on_approval_and_keeps_other_tracks(tmp_path, setup_studio, monkeypatch):
    studio,p,shot,rev,fake = setup_studio
    fake.schema.update({
        "LoadVideo":node_schema({"file":[["example.mp4"]]},["VIDEO"]),
        "GetVideoComponents":node_schema({"video":["VIDEO"]},["IMAGE","AUDIO","FLOAT"]),
        "LoadAudio":node_schema({"audio":[["example.wav"]]},["AUDIO"]),
    })
    clip = tmp_path / "cue.mp4"
    sound = tmp_path / "cue.wav"
    subprocess.run(["ffmpeg","-loglevel","error","-f","lavfi","-i","color=blue:s=64x48:r=24","-frames:v","5",str(clip)],check=True)
    subprocess.run(["ffmpeg","-loglevel","error","-f","lavfi","-i","sine=duration=0.25",str(sound)],check=True)
    audio,video = p.add_asset(sound),p.add_asset(clip)
    packet = p.add_asset(motion_packet(tmp_path / "source.mmh3"))
    fake.card.update(id="packet-1",geometry={"width":128,"height":64,"frames":124,"fps":24})
    take = p.save_take(shot,{"motion_revision":"saved-revision","motion_selector":"output::mmh3/source.mmh3",
                            "delivery_trim":[0,5.0],"seed":123},packet_asset=packet)
    child = p.branch_take(take,"continue")
    tracks = [{"role":"image","frame":0,"revision":rev,"usage":"timed"},
              {"role":"video","frame":0,"asset":video},{"role":"audio","frame":0,"asset":audio},
              {"role":"last","frame":119,"revision":rev}]
    p.set_guides(child,tracks)
    painted = p.save_composition("Painted start",64,48,[])
    args = {"mode":"t2va","shot":child,"prompt":"A river moves.","start_mode":"visual","start_revision":painted}
    def shot_state():
        return Project(p.folder).state()["shots"][child]
    before = shot_state()
    with pytest.raises(ValueError,match="Unknown"):
        engine_action(studio,p,"prepare",{**args,"start_revision":"missing"})
    assert shot_state() == before
    preview = engine_action(studio,p,"prepare",args)
    assert shot_state() == before and not studio.queue.list()
    assert [g["kind"] for g in preview["manifest"]["guides"]] == ["image","video","audio","image"]
    assert preview["manifest"]["guides"][0]["revision"] == painted
    assert preview["manifest"]["start_choice"] == {"mode":"visual","revision":painted}
    def approve(plan):
        return engine_action(studio,Project(p.folder),"submit_prepared",{"plan":plan["plan"]})
    with monkeypatch.context() as fault:
        def reject(*args,**kwargs): raise ValueError("Out of disk space")
        fault.setattr(studio.queue,"enqueue",reject)
        with pytest.raises(ValueError,match="disk"): approve(preview)
    assert shot_state() == before and not studio.queue.list()
    first = approve(preview)
    selected = shot_state()
    assert selected["guides"] == tracks and selected["start_revision"] == painted and selected["start_mode"] == "visual"
    motion = engine_action(studio,p,"prepare",{**args,"start_mode":"original_motion"})
    assert motion["manifest"]["parent"]["take_id"] == take
    assert motion["manifest"]["guides"][0]["revision"] == rev
    assert shot_state() == selected
    approve(motion)
    assert shot_state()["guides"] == tracks and shot_state()["start_revision"] is None
    assert approve(preview)["id"] == first["id"] and shot_state()["start_mode"] == "original_motion"
    stale = engine_action(studio,p,"prepare",args)
    p.set_guides(child,tracks[1:])
    with pytest.raises(ValueError,match="guides changed"): approve(stale)
    assert len(studio.queue.list()) == 2 and shot_state()["guides"] == tracks[1:]


def test_cpu_storyboard_renders_the_approved_visual_start(tmp_path, setup_studio):
    from h3studio.application import Studio
    from h3studio.media import frame_index, extract_frame
    _,p,shot,original,_ = setup_studio
    paint = tmp_path/"painted.png"
    Image.new("RGB",(64,48),"blue").save(paint)
    revision = p.save_composition("Blue",64,48,[{"asset":p.add_asset(paint)}])
    take = p.save_take(shot,{"seed":1})
    child = p.branch_take(take,"continue")
    guides = [{"frame":0,"role":"first_frame","revision":original},{"frame":119,"role":"last","revision":original}]
    p.set_guides(child,guides)
    p.choose_start(child,"visual",revision)
    studio = Studio(tmp_path/"home",p.folder)
    try:
        job = studio.action("storyboard",{"shot":child,"width":64,"height":48})["task"]
        studio.pool.submit(lambda:None).result(30)
        assert studio.tasks[job]["state"] == "completed", studio.tasks[job]
        result = p.state()["takes"][studio.tasks[job]["result"]["take"]]
        assert result["settings"]["start_choice"] == {"mode":"visual","revision":revision}
        assert result["guides"][0]["revision"] == revision and p.state()["shots"][child]["guides"] == guides
        source = p.asset_path(result["video_asset"])
        frames = frame_index(source)
        assert len(frames)==120
        for i,expected in [(0,(0,0,255)),(119,(255,165,0))]:
            path = tmp_path/f"decoded-{i}.png"
            extract_frame(source,frames[i]["pts"],path)
            with Image.open(path) as image:
                pixel = image.convert("RGB").getpixel((32,24))
            assert max(abs(x-y) for x,y in zip(pixel,expected))<=5
    finally:
        studio.close()


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
