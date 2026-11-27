import base64
import hashlib
import io
import json
import re
import shutil
import subprocess
import uuid
import wave

import pytest
from PIL import Image

from h3studio import director, game, game_story
from h3studio.project import Project


def read_story(path):
    html = path.read_text("utf-8")
    text = html.split('<script type="application/json" id="story">')[1].split('</script>', 1)[0]
    script = html.split('<script id="reader">')[1].split('</script>', 1)[0]
    assert "sha256-"+base64.b64encode(hashlib.sha256(script.encode()).digest()).decode() in html
    return json.loads(text), script


def export(project, path, turn, **fields):
    return director.action(None, project, "director_story_share", dict(turn=turn,path=str(path),**fields))


def test_authored_unexplored_moves_and_custom_saved_branches_are_read_only(tmp_path):
    p = Project.create(tmp_path/"repo", "Private project")
    root = game_story._id(p.state()["id"], "Tidal Relay game opening")
    custom = str(uuid.uuid4())
    plate = game_story._visual(p.state())
    with p.transaction() as doc:
        store = director._store(doc)
        store["turns"][root] = dict(action="Begin watch", scene="Hiss.", parent=None, story_node="watch",
                                    game_state=game.initial(relay=True), story_plates=[plate]*3)
        store["turns"][custom] = dict(parent=root,action="Scan the outer reef",scene="Two lights.")
        store["active_turn"] = custom
    before = p.database.read_bytes()
    path = tmp_path/"Game.html"
    r = export(p,path,custom)
    data,_ = read_story(path)
    assert r["scenes"] == 11 and r["images"] == r["voices"] == 0
    assert data["kind"] == "Authored interactive story" and len(data["scenes"][0]["choices"]) == 4
    endings = []
    for _, first in data["scenes"][0]["choices"][:3]:
        for _, tail in data["scenes"][first]["choices"]:
            assert data["scenes"][tail]["ending"]
            endings.append(data["scenes"][tail]["title"])
    assert len(endings) == 6 and len(set(endings)) == 4
    assert data["scenes"][data["scenes"][0]["choices"][-1][1]]["text"] == "Two lights."
    assert p.database.read_bytes() == before
    saved = path.read_bytes()
    with pytest.raises(ValueError, match="new filename"):
        export(p,path,root)
    with pytest.raises(ValueError, match="outside the open project"):
        export(p,p.folder/"story.html",root)
    assert path.read_bytes() == saved and not (p.folder/"story.html").exists()


@pytest.fixture
def saved(tmp_path):
    p = Project.create(tmp_path/"project", "PRIVATE_PROJECT")
    image = tmp_path/"secret-art.png"
    Image.new("RGB",(8,6),"#253744").save(image)
    aid = p.add_asset(image)
    audio = tmp_path/"secret-narrator.wav"
    with wave.open(str(audio),"wb") as out:
        out.setparams((1,2,16000,32,"NONE","not compressed"))
        out.writeframes(b"\x01\x00"*32)
    voice = p.add_asset(audio)
    with p.transaction() as doc:
        turns = director._store(doc)["turns"]
        turns["opening"] = dict(parent=None,action="Start in the fog",scene="Two boats.",actions=["UNPLAYED_SUGGESTION"])
        turns["north"] = dict(parent="opening",action="North",scene="Moon </script><p>rise</p>",
                              scene_visual={"revision":"missing-old-guide","poster":aid})
        turns["east"] = dict(parent="opening",action="East",scene="An inlet.")
        turns["secret"] = dict(parent=None,action="PRIVATE_GAME",scene="PRIVATE_CHARACTER")
        turns["second"] = dict(parent="secret",action="PRIVATE_TURN",scene="Do not export.")
        doc["director"]["reviews"]["token"] = {"key":"PRIVATE_API_KEY"}
        doc["narrations"] = {"yes":dict(id="yes",source={"turn":"north"},text="Moonrise.",asset=voice),
                             "no":dict(id="no",source={"turn":"secret"},text="PRIVATE_VOICE",asset=voice)}
    return p


def test_export_embeds_only_selected_episode_pixels_and_opted_in_pcm(saved,tmp_path):
    silent = tmp_path/"silent.html"
    voiced = tmp_path/"voiced.html"
    export(saved,silent,"north")
    stats = export(saved,voiced,"east",include_voice=True)
    quiet,_ = read_story(silent)
    data,_ = read_story(voiced)
    assert data["kind"] == "Saved Game branches" and [edge[0] for edge in data["scenes"][0]["choices"]] == ["North","East"]
    assert stats["scenes"] == 3 and len(data["images"]) == stats["images"] == 1
    encoded = data["images"][0].partition(",")[2]
    with Image.open(io.BytesIO(base64.b64decode(encoded))) as im:
        assert im.format == "JPEG" and im.size == (8,6) and not im.getexif()
    assert quiet["sounds"] == [] and all(s["voice"] is None for s in quiet["scenes"])
    assert stats["voices"] == 1 and [s["voice"] for s in data["scenes"]] == [None,0,None]
    with wave.open(io.BytesIO(base64.b64decode(data["sounds"][0].split(",")[1])),"rb") as wav:
        assert wav.readframes(32) == b"\x01\x00"*32
    raw = voiced.read_text("utf-8")
    for token in ("PRIVATE_", "secret-", "UNPLAYED_SUGGESTION", str(saved.folder), "Moon </script>"):
        assert token not in raw
    assert data["scenes"][1]["text"] == "Moon </script><p>rise</p>"
    assert " autoplay" not in raw and not re.search(r'(src|href)="(?:https?|file):', raw)


def test_two_responses_to_same_move_and_private_prose(saved,tmp_path):
    with saved.transaction() as doc:
        turns=doc["director"]["turns"]
        turns["another"]=dict(parent="opening",action="East",scene="Follow the stars.")
        turns["north"]["scene"]="Check the engine. Bearer abcdefghijklmnop\n"+str(saved.folder/"private.txt")
    output = tmp_path/"branches.html"
    export(saved,output,"another")
    data,_ = read_story(output)
    labels = [label for label,_ in data["scenes"][0]["choices"]]
    assert labels == ["North","East · Branch 1","East · Branch 2"]
    assert "abcdefghijklmnop" not in data["scenes"][1]["text"]
    assert str(saved.folder) not in data["scenes"][1]["text"]


def test_media_over_budget_leaves_a_playable_reader_with_skip_counts(saved,tmp_path,monkeypatch):
    from h3studio import game_share
    monkeypatch.setattr(game_share,"MIB",20)
    path=tmp_path/"compact.html"
    stats=export(saved,path,"east",include_voice=True)
    data,_=read_story(path)
    assert stats["skipped_images"] == stats["skipped_voices"] == 1
    assert not data["images"] and not data["sounds"] and len(data["scenes"]) == 3


@pytest.mark.skipif(not shutil.which("node"), reason="Node runs the portable reader fixture")
def test_reader_keyboard_back_replay_and_audio_stop(saved,tmp_path):
    path = tmp_path/"read.html"
    export(saved,path,"north",include_voice=True)
    data, script = read_story(path)
    # Execute the actual shipped reader with only the DOM methods it uses; no window is opened.
    fixture = """
const assert=require('node:assert/strict');
class Element {
  constructor(){this.children=[];this.classList={toggle(){}};}
  replaceChildren(){this.children=[];} append(x){this.children.push(x);}
  get childElementCount(){return this.children.length;}
  setAttribute(k,v){this[k]=v;} removeAttribute(k){delete this[k];}
  focus(){document.activeElement=this;} pause(){this.paused=true;} load(){} click(){this.onclick();}
}
const elements=new Map();
const document={getElementById(k){if(!elements.has(k))elements.set(k,new Element());return elements.get(k);},
createElement(){return new Element();},createTextNode(t){return t;},
addEventListener(k,fn){this[k]=fn;}};
document.getElementById('story').textContent=DATA;
"""
    assertions = """
const key=n=>document.keydown({key:String(n),preventDefault(){}});
assert.equal(el('heading').textContent,'Start in the fog');
assert.equal(el('choices').childElementCount,2);assert.ok(el('back').disabled);
key(1);assert.equal(el('heading').textContent,'North');assert.equal(el('end').textContent,'End of this saved branch.');
assert.ok(!el('voice').hidden&&el('voice').src.startsWith('data:audio/wav'));
el('back').click();assert.ok(el('voice').hidden&&el('voice').paused&&!el('voice').src);
key(2);assert.equal(el('heading').textContent,'East');assert.equal(document.activeElement,el('heading'));
el('replay').click();assert.equal(el('heading').textContent,'Start in the fog');assert.ok(el('replay').disabled);
"""
    subprocess.run([shutil.which("node")], input=fixture.replace("=DATA;","="+json.dumps(json.dumps(data))+";")+
                   script+assertions, text=True,encoding="utf-8",check=True,capture_output=True,timeout=8)
