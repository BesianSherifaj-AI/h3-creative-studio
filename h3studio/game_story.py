"""Playable authored Game turns, saved viewpoints and portable Game-to-Film storyboards."""
from __future__ import annotations

import copy
import math
import shutil
import uuid
from bisect import bisect_right
from fractions import Fraction

from . import cut, game


def _node(title, scene, plate, choices=(), changes=(), effects=(), ending=False):
    return dict(title=title, scene=scene, plate=plate, choices=choices, changes=changes, effects=effects, ending=ending)


# The example clips establish locations. The player's decision and its consequences are authored text.
RELAY = {
    "watch": _node("The night watch", "", 0, (
        ("answer","Answer with the station call sign"),("trace","Trace the signal on the receiver"),
        ("shore","Take the field lamp to the breakwater"))),
    "answer": _node("A voice beneath the static",
        "“Relay Nine, come in.” The shore keeper recognizes her brother’s voice. His pilot launch has lost its lights. "
        "She holds out the microphone. Another ship is already calling on this channel.", 1,
        (("rescue","Clear the channel for the pilot launch"),("beacon","Lead them in with the field lamp")),
        ("The shore keeper identifies the missing launch.",)),
    "trace": _node("Two bearings",
        "You narrow the receiver's bandwidth. Two ticks separate into a ship's call and its echo from the outer buoy. "
        "The keeper unfolds a damp chart. A grease-pencil cross marks a gap in the reef.", 1,
        (("chart","Log both bearings and sound the harbour bell"),("rescue","Broadcast the clear bearing to the launch")),
        ("Two bearings locate the launch at the outer buoy.",)),
    "shore": _node("At the waterline",
        "You carry the brass field lamp down the wet steps. The keeper stays at the radio. Between the crests, "
        "a white light rises and drops again. It is close enough to answer.", 2,
        (("beacon","Signal the safe passage with the field lamp"),("silence","Shield the lamp and listen")),
        ("Field lamp acquired.","You reach the breakwater."),
        ({"kind":"holder","entity_id":"lamp","character_id":"player"},
         {"kind":"character_location","character_id":"player","location_id":"breakwater"})),
    "rescue": _node("The home channel",
        "You transmit the bearing. The keeper counts the seconds until a tired voice reads it back. By dawn "
        "the pilot launch lies against the quay. In the log, she signs her brother’s name beneath yours.", 1,
        changes=("The pilot launch returned by radio guidance.",), ending=True),
    "beacon": _node("A light to steer by",
        "Through the spray, your lamp draws a slow figure eight. The launch answers with two flashes and clears "
        "the reef. From the station, the keeper rings the all-clear.", 2,
        changes=("The pilot launch followed the lamp home.",), ending=True),
    "chart": _node("On the morning chart",
        "The harbour bell carries across the fog. A tug takes the bearings over the radio and reaches the launch. "
        "At first light, the keeper inks a new approach line on the chart.", 0,
        changes=("The tug reached the launch. A safe approach was charted.",), ending=True),
    "silence": _node("The listening post",
        "With the lamp shielded, you hear oars striking stone. You call down to the crew under the seawall. "
        "The pilot climbs your ladder first. The launch stays below until morning.", 2,
        changes=("The crew reached shelter at the seawall.",), ending=True),
}


def _id(ident, name):
    return str(uuid.uuid5(uuid.UUID(ident), name))


def _visual(doc, shot_id=None, take_id=None):
    shot=doc["shots"].get(shot_id,{})
    take=doc["takes"].get(take_id or shot.get("selected_take"),{})
    guides=take.get("guides",shot.get("guides",[]))
    guide=next((g for g in guides if g.get("revision") in doc["revisions"] or
                g.get("asset") in doc["assets"] and g.get("role") in ("image","first","first_frame","reference_image")),{})
    return dict(shot=take.get("shot") or shot.get("id"),take=take.get("id"),video=take.get("video_asset"),
                revision=guide.get("revision"),poster=guide.get("asset") or take.get("ending_asset"))


def seed(doc,store):
    """Upgrade a 2.0 starter lazily, pinning the original authored takes even after edits."""
    root=_id(doc["id"],"Tidal Relay game opening")
    board=store["boards"].get(_id(doc["id"],"Tidal Relay example board"),{})
    turn=store["turns"].get(root)
    if turn and board.get("author")=="Studio example" and len(board["rows"])==3 and "story_plates" not in turn:
        plates=[]
        for row in board["rows"]:
            shot=doc["shots"].get(row["shot"],{})
            original=next((k for k in shot.get("takes",[]) if doc["takes"][k]["settings"].get("kind")=="Studio animatic"),None)
            plates.append(_visual(doc,shot.get("id"),original))
        turn.update(story_node="watch",story_plates=plates,scene_visual=plates[0])
    return root


def _lineage(store,ident):
    chain=[]
    while ident:
        if ident in chain or ident not in store["turns"]:
            raise ValueError("Choose a saved Game branch.")
        chain.append(ident)
        ident=store["turns"][ident].get("parent")
    if not chain:
        raise ValueError("Choose a saved Game turn.")
    return chain[::-1]


def visual(doc,turn):
    if shot:=doc["shots"].get(turn.get("shot")):
        result=_visual(doc,shot["id"])
        if result["video"] or result["revision"] or result["poster"]:
            return result
    return turn.get("scene_visual") or _visual(doc,turn.get("reference_shot"),turn.get("source_take"))


def _scene_visual(doc,store,path):
    for ident in reversed(path):
        item=visual(doc,store["turns"][ident])
        if item["video"] or item["revision"] or item["poster"]:
            return dict(item,from_turn=ident)
    return dict(_visual(doc),from_turn=None)


def _film_placements(doc,store,turn_id):
    """A location plate may appear several times; resolve editable shots by their Game turn."""
    places=[]
    for shot_id in doc["cut"]:
        shot=doc["shots"][shot_id]
        board_id=shot.get("director_board")
        board=store["boards"].get(board_id,{})
        if shot.get("game_turn")==turn_id and board.get("game_turn"):
            places.append(dict(shot=shot_id,take=shot.get("selected_take"),board=board_id,
                               title=board["title"]+" · "+shot["name"]))
    preferred=store.get("last_game_film")
    places.sort(key=lambda placement:placement["board"]!=preferred)
    return places


def _view(doc,store,path):
    ident=path[-1]
    turn=store["turns"][ident]
    node=RELAY.get(turn.get("story_node"))
    v=game.view(game.snapshot(turn))
    return dict(turn=ident,title=node["title"] if node else turn["action"],node=turn.get("story_node"),
        ending=bool(node and node["ending"]),choices=[dict(id=i,label=s) for i,s in node["choices"]] if node else [],
        visual=_scene_visual(doc,store,path),film_placements=_film_placements(doc,store,ident),actor=v["context"]["actor"]["name"],
        place=(v["context"]["location"] or {}).get("name"),inventory=[e["name"] for e in v["inventory"]],
        objectives=[dict(name=o["name"],status=o["status"]) for o in v["context"]["objectives"]],
        waypoints=[b for b in store.get("waypoints",{}).values() if b["root"]==path[0]])


def _choose(doc,store,path,args):
    parent=store["turns"][path[-1]]
    current=RELAY.get(parent.get("story_node"))
    selected=args.get("choice")
    if not current or selected not in dict(current["choices"]):
        raise ValueError("Choose an available move from this scene.")
    ident=_id(path[-1],"Tidal Relay choice "+selected)
    if ident not in store["turns"]:
        node=RELAY[selected]
        effects=list(node["effects"])
        if selected=="beacon" and parent["story_node"]=="answer":
            effects=list(RELAY["shore"]["effects"])
        if node["ending"]:
            effects.append({"kind":"objective","objective_id":"signal","status":"completed"})
        draft=dict(scene=node["scene"],state_changes=list(node["changes"]),outcome="resolved",
                   actions=[label for _,label in node["choices"]])
        saved=game.outcome(parent,{},dict(draft,world_effects=effects),ident)
        plate=store["turns"][path[0]]["story_plates"][node["plate"]]
        store["turns"][ident]=dict(draft,parent=path[-1],action=dict(current["choices"])[selected],author="Tidal Relay",
            game_state=saved,story_node=selected,scene_visual=copy.deepcopy(plate),reference_shot=plate["shot"])
    store["active_turn"]=ident
    return {"turn":ident}


def _waypoint(project,doc,store,path,args):
    from .playback import source_window
    item=_scene_visual(doc,store,path)
    if args.get("waypoint"):
        saved=store.get("waypoints",{}).get(args["waypoint"])
        if not saved or saved["turn"]!=path[-1]:
            raise ValueError("Choose a saved viewpoint for this scene.")
        item=saved["visual"]
    if args.get("take")!=item["take"]:
        raise ValueError("This scene's take changed. Reopen the player before saving.")
    label=game._label(args.get("name"),limit=100) or RELAY.get(store["turns"][path[-1]].get("story_node"),{}).get("title") or store["turns"][path[-1]]["action"][:100]
    seconds=args.get("seconds",0)
    if type(seconds) not in (int,float) or not math.isfinite(seconds) or seconds<0:
        raise ValueError("Choose a visible time in this scene.")
    frame=pts=None
    seek_seconds=seconds
    if item["video"]:
        take=doc["takes"][item["take"]]
        if seconds>take["duration"]+.05:
            raise ValueError("This viewpoint is beyond the end of the scene.")
        source=project._asset_path(doc,item["video"])
        if shutil.which("ffprobe"):
            frames,times,_,a,b,first,last=source_window(source,take["settings"].get("delivery_trim"))
            frame=max(first,min(last,bisect_right(times,a+Fraction(str(seconds))+Fraction(1,1_000_000))-1))
            pts=frames[frame]["pts"]
            seconds=float(max(a,times[frame])-a)
            # Seek within the saved sample even when its exact start cannot be represented by WebView's clock.
            end=min(b,times[frame+1]) if frame<last else b
            seek_seconds=float((max(a,times[frame])+end)/2-a)
    else:
        seconds=0
    entry=dict(root=path[0],turn=path[-1],name=label,visual=item,seconds=seconds,seek_seconds=seek_seconds,source_frame=frame,source_pts=pts,
               world_revision=game.fingerprint(game.snapshot(store["turns"][path[-1]])))
    key=_id(path[-1],str((item["take"],item["revision"],seconds,label)))
    entry["id"]=key
    points=store.setdefault("waypoints",{})
    if points.get(key,{}).get("motion_shot"): entry["motion_shot"]=points[key]["motion_shot"]
    points[key]=entry
    return {"waypoint":entry}


def _board(project,doc,store,path):
    """Use the chosen branch only. Attach independent, editable copies of its actual location clips."""
    visuals=[_scene_visual(doc,store,path[:i+1]) for i in range(len(path))]
    signature="|".join(str((v["take"],v["revision"])) for v in visuals)
    key=_id(path[-1],"Game branch storyboard "+signature)
    store["last_game_film"]=key
    if key in store["boards"]:
        shots=[r["shot"] for r in store["boards"][key]["rows"]]
        if any(ident not in doc["cut"] for ident in shots):
            before=cut._snapshot(doc)
            project._remember_cut(doc)
            for i,ident in enumerate(shots):
                if ident in doc["cut"]: continue
                following=next((x for x in shots[i+1:] if x in doc["cut"]),None)
                prior=next((x for x in reversed(shots[:i]) if x in doc["cut"]),None)
                index=len(doc["cut"])
                if following:
                    index=doc["cut"].index(following)
                elif prior:
                    index=doc["cut"].index(prior)+1
                doc["cut"].insert(index,ident)
            cut._remember(doc,before,"Restore Game branch to film")
        return {"board":key,"shots":shots}
    rows=[]
    before=cut._snapshot(doc)
    project._remember_cut(doc)
    for i,(ident,item) in enumerate(zip(path,visuals)):
        turn=store["turns"][ident]
        node=RELAY.get(turn.get("story_node"),{})
        title=node.get("title") or turn["action"][:180]
        prompt=game.visual_prompt(turn)
        src=doc["takes"].get(item["take"],{})
        if item["from_turn"]!=ident or turn.get("inherited_visual") and not doc["shots"].get(turn.get("shot"),{}).get("selected_take"):
            src={}
        shot=project._shot(title,src.get("duration") or 5)
        shot.update(game_turn=ident,director_board=key,director_row=i,director_prompt=prompt)
        if src.get("video_asset"):
            take=copy.deepcopy(src)
            take.update(id=uuid.uuid4().hex,shot=shot["id"],parent_take=src["id"])
            if job:=take["settings"].pop("job_id",None):
                take["settings"]["source_job_id"]=job
            doc["takes"][take["id"]]=take
            shot.update(selected_take=take["id"],takes=[take["id"]],guides=copy.deepcopy(take["guides"]))
        elif item["revision"] or item["poster"]:
            guide=dict(role="image",usage="both",frame=0)
            if item["revision"]: guide["revision"]=item["revision"]
            else: guide["asset"]=item["poster"]
            shot["guides"]=[guide]
        doc["shots"][shot["id"]]=shot
        doc["cut"].append(shot["id"])
        rows.append(dict(title=title,prompt=prompt,duration=shot["duration"],shot=shot["id"],renders=[],game_turn=ident))
    ending=rows[-1]["title"]
    store["boards"][key]=dict(title="Game · "+ending,author="Game branch",game_turn=path[-1],rows=rows)
    cut._remember(doc,before,"Add Game branch to film")
    return {"board":key,"shots":[r["shot"] for r in rows]}


def _motion_from_view(project,args):
    from . import editor
    doc=project.state()
    mark=doc.get("director",{}).get("waypoints",{}).get(args.get("waypoint"))
    if not mark or mark["turn"]!=args.get("turn"):
        raise ValueError("Save or restore a viewpoint on this Game turn first.")
    if mark.get("motion_shot") in doc["shots"]:
        return {"shot":mark["motion_shot"]}
    item=mark["visual"]
    revision=item["revision"]
    source=None
    if item["video"]:
        selection={"source_index":mark["source_frame"]} if mark["source_frame"] is not None else {"seconds":mark["seconds"]}
        take,path,frame=editor._selection(project,dict(take=item["take"],**selection))
        if mark["source_pts"] is not None and frame["pts"]!=mark["source_pts"]:
            raise ValueError("This frame no longer matches the saved source.")
        if max(frame["width"],frame["height"])>8192 or frame["width"]*frame["height"]>24_000_000:
            raise ValueError("The layer canvas supports up to 8192 pixels per side and 24 megapixels.")
        asset=editor._snapshot(project,path,frame)
        revision=project.save_composition(mark["name"]+" · frame "+str(frame["source_index"]),
            frame["width"],frame["height"],[dict(asset=asset,name="Game viewpoint")],
            job_id=_id(mark["id"],"motion guide"))
        source=dict(take=take["id"],source=take["video_asset"],pts=frame["pts"],
                    index=frame["source_index"],time_base=frame["time_base"],base_revision=revision)
    if not revision and not item["poster"]:
        raise ValueError("Choose a viewpoint with a saved frame or guide.")
    with project.transaction() as doc:
        saved=doc["director"]["waypoints"][mark["id"]]
        if saved.get("motion_shot") in doc["shots"]:
            return {"shot":saved["motion_shot"]}
        before=cut._snapshot(doc)
        project._remember_cut(doc)
        if source:
            composition=doc["revisions"][revision]["composition"]
            doc.setdefault("source_frames",{})[composition]=source
        prompt=game.visual_prompt(doc["director"]["turns"][mark["turn"]])
        guide=dict(role="image",frame=0,usage="both",**({"revision":revision} if revision else {"asset":item["poster"]}))
        shot=project._shot(mark["name"]+" · from viewpoint",5)
        shot.update(game_turn=mark["turn"],game_waypoint=mark["id"],director_prompt=prompt,
                    recipe=dict(mode="i2va",prompt=prompt),guides=[guide])
        doc["shots"][shot["id"]]=shot
        doc["cut"].append(shot["id"])
        saved["motion_shot"]=shot["id"]
        cut._remember(doc,before,"Create Motion shot from Game viewpoint")
        return {"shot":shot["id"]}


def action(project,op,args):
    from .director import _store
    if op=="director_story_share":
        from .game_share import export
        return export(project,args)
    if op=="director_story_motion": return _motion_from_view(project,args)
    if op=="director_story_view":
        doc=project.state()
        store=_store(doc)
        seed(doc,store)
        return _view(doc,store,_lineage(store,args.get("turn") or store["active_turn"]))
    with project.transaction() as doc:
        store=_store(doc)
        seed(doc,store)
        path=_lineage(store,args.get("turn") or store["active_turn"])
        if op=="director_story_choose": return _choose(doc,store,path,args)
        if op=="director_story_bookmark": return _waypoint(project,doc,store,path,args)
        if op=="director_story_film": return _board(project,doc,store,path)
        if op=="director_story_restore":
            entry=store.get("waypoints",{}).get(args.get("waypoint"))
            if not entry or entry["root"]!=path[0]: raise ValueError("Choose a saved viewpoint in this game.")
            store["active_turn"]=entry["turn"]
            return {"turn":entry["turn"],"waypoint":entry}
        raise ValueError("Choose a supported story action.")
