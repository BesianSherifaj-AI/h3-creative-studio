import copy
import json
import uuid

import pytest
from h3studio import director, game
from h3studio.project import Project
from test_director import setup


@pytest.fixture
def relay(setup):
    p,studio,call,reply=setup
    root=director.seed_example(p,[p.add_shot(s,5) for s in ("Station","Radio","Breakwater")])["turn"]
    def play(parent,intent=None,answer=None,**fields):
        job=call("director",mode="game",parent=parent,game_intent=intent,prompt="Play",**fields)
        result=reply(job,{"scene":"The receiver chimes.","actions":["Wait"],"state_changes":[],**(answer or {})})
        call("director_approve",review=result)
        return result
    return p,studio,call,reply,root,play


def test_grounded_inventory_and_independent_siblings(relay):
    p,studio,call,reply,root,play=relay
    view=lambda key:call("director_world",turn=key)
    held=play(root,{"kind":"take","target_id":"lamp"},world_revision=view(root)["revision"])
    denied=play(root,{"kind":"take","target_id":"lamp"},{"outcome":"blocked"})
    assert [i["name"] for i in view(held)["inventory"]]==["Field lamp"]
    assert view(root)["inventory"]==view(denied)["inventory"]==[]
    passed=play(held,{"kind":"give","target_id":"lamp","recipient_id":"keeper"})
    assert view(passed)["inventory"]==[]
    lamp=next(e for e in view(passed)["context"]["visible_objects"] if e["id"]=="lamp")
    assert lamp["holder_id"]=="keeper"
    with pytest.raises(ValueError,match="holding"):
        play(root,{"kind":"give","target_id":"lamp","recipient_id":"keeper"})
    with pytest.raises(ValueError,match="changed"):
        play(held,{"kind":"look"},world_revision=view(root)["revision"])
    payload=studio.queue.get(passed,full=True)["payload"]
    assert "Shore keeper" in payload["director_request"]["brief"]
    assert "Proposed mechanical effects" in payload["prompt"] and "Field lamp" in payload["prompt"]
    assert view(held)["revision"]==game.fingerprint(Project(p.folder).state()["director"]["turns"][held]["game_state"])


def test_positions_camera_travel_h3_and_discovery(relay):
    p,studio,call,reply,root,play=relay
    step=play(root,{"kind":"move","direction":"left","extent":"nearby"})
    camera=play(step,{"kind":"move","direction":"forward","camera":"camera","speed":"slow"})
    v=call("director_world",turn=camera)
    assert v["navigation"]["player"]==[-3,0] and v["navigation"]["camera"]==[0,1]
    arrived=play(camera,{"kind":"move","target_id":"breakwater","extent":"travel"},
        {"discoveries":{"entities":[{"id":"shell","name":"White shell","location_id":"breakwater"}]}})
    v=call("director_world",turn=arrived)
    assert v["context"]["location"]["name"]=="Breakwater" and v["navigation"]["player"]==[0,0]
    assert v["actions"]["targets"][0]["name"]=="White shell"
    jid=call("director_game_render",turn=camera,request_id=str(uuid.uuid4()))["id"]
    prompt=p.state()["prepared"][jid.replace("-","")]["request"]["prompt"]
    assert "Only the viewpoint moves" in prompt and "dark wool coat" in prompt
    assert call("director_world",turn=root)["context"]["location"]["name"]=="Listening station"


def test_character_world_edits_fork_and_retry(relay):
    p,studio,call,reply,root,play=relay
    v=lambda t:call("director_world",turn=t)
    def change(turn,k,**a):
        fields=dict(turn=turn,revision=v(turn)["revision"],request_id=str(uuid.uuid4()),edit=k,**a)
        result=call("director_world_edit",**fields)["turn"]
        assert call("director_world_edit",**fields)["turn"]==result
        return result
    player=change(root,"player",player="keeper")
    assert v(root)["context"]["actor"]["id"]=="player" and v(player)["context"]["actor"]["id"]=="keeper"
    named=change(player,"identity",name="Mira",description="A striped jacket",personality="Curious",voice="Quiet")
    ruled=change(named,"rules",description="The exit floods at high tide.\nLights change every hour.")
    place=change(ruled,"location",name="Lantern room")
    prop=change(place,"item",name="Notebook")
    npc=change(prop,"character",name="Tomas")
    final=change(npc,"objective",name="Read the log")
    own=v(final)
    assert own["context"]["actor"]["name"]=="Mira" and len(own["context"]["rules"])==2
    assert {"Notebook","Tomas","Lantern room"}.issubset({r["name"] for r in own["actions"]["targets"]})
    assert "Read the log" in [o["name"] for o in own["context"]["objectives"]]
    assert Project(p.folder).state()["director"]["active_turn"]==final
    assert all(r["name"]!="Notebook" for r in v(root)["actions"]["targets"])


def test_custom_opening_selected_profiles_and_freeform_effects(relay):
    p,studio,call,reply,root,play=relay
    studio.prefs.update(film_director_model="cinema exact",game_director_model="adventure exact",
        game_director_url="http://127.0.0.1:2345",game_director_instructions="Write in second person.")
    film=call("director",mode="film",prompt="Harbor",count=1)
    opening=call("director",mode="game",prompt="Open the gate",game_setup={"name":"Esther","location":"South quay"})
    packet=studio.queue.get(opening["id"],full=True)["payload"]
    assert packet["model"]=="adventure exact" and packet["url"].endswith(":2345") and "second person" in packet["prompt"]
    assert studio.queue.get(film["id"],full=True)["payload"]["model"]=="cinema exact"
    key=reply(opening,{"scene":"A parcel rests on the quay.","discoveries":{"entities":[{"id":"parcel","name":"Parcel"}]}})
    call("director_approve",review=key)
    turn=play(key,answer={"world_effects":[{"kind":"holder","entity_id":"parcel","character_id":"player"}]})
    v=call("director_world",turn=turn)
    assert v["context"]["actor"]["name"]=="Esther" and v["context"]["location"]["name"]=="South quay"
    assert v["inventory"][0]["name"]=="Parcel"
    conflict=reply(call("director",mode="game",parent=root,game_intent={"kind":"take","target_id":"lamp"}),
                   {"scene":"Conflicting result","world_effects":[{"kind":"holder","entity_id":"lamp","character_id":"keeper"}]})
    assert "disagree" in p.state()["director"]["reviews"][conflict]["error"]
    call("director_approve",review=conflict,correction=json.dumps({"scene":"The operator lifts the lamp."}))
    assert call("director_world",turn=conflict)["inventory"][0]["name"]=="Field lamp"


def test_import_only_exact_matching_historical_snapshots(relay):
    p,studio,call,reply,root,play=relay
    world=copy.deepcopy(p.state()["director"]["turns"][root]["game_state"]["world"])
    current=copy.deepcopy(world)
    current["entities"][0]["holder_id"]="player"
    source={"world":current,"player_character_id":"player","navigation":{"position":[3,2]}}
    history={"world":world,"player_character_id":"player","navigation":{"position":[1,0]}}
    legacy={"documents":{"stories/relay.json":{"document":{"id":"relay","title":"Relayed","mode":"game",**source,
        "active_run_id":"c","active_branch_id":"east","state_by_run":{"a":history},
        "turns":[{"run_id":s,"plan":{"action":"Scene "+s}} for s in "abc"]}}},
        "cuts":{"story:relay:east":[{"legacy_run":s,"take":None} for s in "ac"],
                "story:relay:west":[{"legacy_run":s,"take":None} for s in "ab"]}}
    with p.transaction() as doc:doc["legacy"]=legacy
    east=call("director_resume_import",story="relay",branch="east")["turn"]
    west=call("director_resume_import",story="relay",branch="west")["turn"]
    common=p.state()["director"]["turns"][east]["parent"]
    assert call("director_world",turn=east)["inventory"][0]["id"]=="lamp"
    assert call("director_world",turn=common)["navigation"]["player"]==[1,0]
    assert call("director_world",turn=west)["inventory"]==[]
