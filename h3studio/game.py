"""Game turn snapshots, grounded controls and world-edit branches."""
from __future__ import annotations

import copy
import hashlib
import json
import uuid

from vendor.prompt import world as engine


def _label(value, default="", limit=180):
    if value is None:
        return default
    if not isinstance(value, str) or len(value)>limit:
        raise ValueError(f"Use text up to {limit} characters.")
    return value.strip() or default


def initial(setup=None, *, relay=False):
    setup=setup or {}
    actor=_label(setup.get("name"), "Relay operator" if relay else "Player")
    place=_label(setup.get("location"), "Listening station" if relay else "Opening scene")
    world={"schema_version":1,"current_location_id":"opening",
           "characters":[{"id":"player","name":actor,"description":_label(setup.get("description"),limit=2400),
                          "control":"player","location_id":"opening"}],
           "locations":[{"id":"opening","name":place,"exits":[]}],"entities":[]}
    if relay:
        world["characters"][0]["description"]="A night operator wearing a dark wool coat."
        world["characters"].append({"id":"keeper","name":"Shore keeper","description":"An older keeper with a salt-stained cap.",
                                    "location_id":"opening","speaking_style":"Brief, precise answers."})
        world["locations"][0]["exits"]=["breakwater"]
        world["locations"].append({"id":"breakwater","name":"Breakwater","description":"A stone path along the outer harbour.","exits":["opening"]})
        world["entities"]=[{"id":"lamp","name":"Field lamp","description":"A brass lamp with a carrying loop.","kind":"tool",
                            "location_id":"opening","affordances":["examine","take","drop","give"]},
                           {"id":"receiver","name":"Radio receiver","kind":"fixture","description":"Its tuning needle jumps twice.",
                            "location_id":"opening","affordances":["examine","touch"]}]
        world["objectives"]=[{"id":"signal","name":"Find the source of the repeating signal"}]
    return {"world":engine.validate_world(world),"player":"player","navigation":{"player":[0,0],"camera":[0,0]}}


def snapshot(turn):
    saved=turn.get("game_state") if turn else None
    if saved:
        return copy.deepcopy(saved)
    return initial(relay=bool(turn and turn.get("author")=="Studio example" and turn.get("reference_shot")))


def fingerprint(state):
    return hashlib.sha256(json.dumps(state,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()


def view(state):
    world,actor=state["world"],state["player"]
    context=engine.actor_context(world,actor)
    return {"revision":fingerprint(state), "context":context, "navigation":state["navigation"],
            "actions":engine.available_actions(world,actor),
            "cast":[{"id":c["id"],"name":c["name"]} for c in world["characters"]],
            "inventory":[e for e in context["visible_objects"] if actor in (e.get("holder_id"),e.get("worn_by_id"))]}


def propose(turn,args,brief):
    state=snapshot(turn) if turn else initial(args.get("game_setup"))
    v=view(state)
    if args.get("world_revision") and args["world_revision"]!=v["revision"]:
        raise ValueError("The Game world changed. Reopen its controls.")
    info={"world_revision":v["revision"]}
    if not turn:
        info["initial_state"]=state
    text="Actor's accepted world: "+json.dumps(v["context"],ensure_ascii=False)+"\nLogical movement coordinates: "+json.dumps(v["navigation"])
    move=args.get("game_intent")
    if move is None:
        match=next((a for a in v["actions"]["actions"] if a["enabled"] and a["kind"]!="give" and a["label"].casefold()==brief.casefold()),None)
        if match: move={k:match[k] for k in ("kind","target_id") if k in match}
    if move is not None:
        r=engine.resolve_intent(state["world"],state["player"],move)
        info["resolved"]=r
        brief=r["action"]
        if r["intent"].get("kind")=="give":
            recipient=next(c["name"] for c in state["world"]["characters"] if c["id"]==r["intent"]["recipient_id"])
            brief+=" to "+recipient
        text+="\nProposed mechanical effects: "+json.dumps(r["effects"])
    return brief,info,text


def _move(navigation,move,effects):
    nav=copy.deepcopy(navigation)
    if move.get("kind")!="move":
        return nav
    subject=move.get("camera","player")
    direction=move.get("direction","forward")
    if any(e["kind"]=="character_location" for e in effects):
        return {"player":[0,0],"camera":[0,0]}
    if direction and not move.get("target_id"):
        dx,dy={"left":(-1,0),"right":(1,0),"forward":(0,1),"backward":(0,-1)}[direction]
        x,y=nav[subject]
        n=3 if move.get("extent")=="nearby" else 1
        nav[subject]=[x+n*dx,y+n*dy]
    nav[subject+"_target"]=move.get("target_id")
    return nav


def outcome(turn,intent,draft,key):
    state=snapshot(turn) if turn else copy.deepcopy(intent.get("initial_state") or initial())
    if intent.get("world_revision") and fingerprint(state)!=intent["world_revision"]:
        raise ValueError("The parent world changed since this request. Request the turn again.")
    resolved=intent.get("resolved") or {}
    passed=draft.get("outcome","resolved")=="resolved"
    try:
        world=engine.apply_discoveries(state["world"],draft.get("discoveries"),state["player"])
        proposed=engine.validate_effects(world,draft.get("world_effects",[]),state["player"])
    except (KeyError,TypeError) as exc:
        raise ValueError("Review the discovery and world_effects fields. Use text IDs for existing records.") from exc
    if resolved and proposed and proposed!=resolved["effects"]:
        raise ValueError("The Director effects disagree with this player control. Review the world_effects field.")
    effects=(resolved.get("effects",proposed) if passed else [])
    if effects and not resolved:
        known=engine.actor_context(world,state["player"],recent_limit=0)
        visible={state["player"],None}
        for name in ("visible_objects","visible_characters","objectives"):
            visible.update(x["id"] for x in known[name])
        if place:=known["location"]:
            visible.add(place["id"])
            visible.update(e["target_id"] for e in place["exits"] if not e.get("locked"))
        if any(e[k] not in visible for e in effects for k in ("character_id","entity_id","objective_id","location_id") if k in e):
            raise ValueError("A proposed effect references something outside this playable scene.")
    state["world"]=engine.apply_effects(world,effects,event_id=key,actor_id=state["player"],summary=draft["scene"])
    if passed:
        movement=resolved.get("intent") or ({"kind":"move"} if any(e["kind"]=="character_location" and e["character_id"]==state["player"] for e in effects) else {})
        state["navigation"]=_move(state["navigation"],movement,effects)
    return state


def visual_prompt(turn):
    scene=turn["scene"]
    if move:=turn.get("resolved_action"):
        if turn.get("outcome")=="resolved":
            scene+="\nVisible action: "+move["action"]
    if saved:=turn.get("game_state"):
        state=engine.actor_context(saved["world"],saved["player"],recent_limit=0)
        actor=state["actor"]
        if actor["description"]:
            scene+="\n"+actor["name"]+" appears as "+actor["description"]
        if place:=state["location"]:
            scene+="\nLocation: "+place["name"]+". "+place.get("description","")
        held=[item["name"] for item in state["visible_objects"] if actor["id"] in (item.get("holder_id"),item.get("worn_by_id"))]
        if held: scene+="\nCarried by "+actor["name"]+": "+", ".join(held)+"."
    return scene


def edit(store,args):
    """A world edit makes a new playable branch and leaves the selected source intact."""
    source=store["turns"][args["turn"]]
    state=snapshot(source)
    ticket=str(uuid.UUID(args["request_id"]))
    if ticket in store["turns"]:
        old=store["turns"][ticket]
        if old.get("parent")==args["turn"] and old.get("world_edit")==args:
            return {"turn":ticket}
        raise ValueError("This edit identifier belongs to another change.")
    if args["revision"]!=fingerprint(state):
        raise ValueError("The world changed. Reopen the selected turn before editing.")
    world=state["world"]
    actor=next(c for c in world["characters"] if c["id"]==state["player"])
    here=actor["location_id"]
    operation=args["edit"]
    name=_label(args.get("name"))
    description=_label(args.get("description"),limit=2400)
    if operation in ("character","location","item","identity","objective") and not name:
        raise ValueError("Enter a name for this change.")
    ident=str(uuid.uuid5(uuid.UUID(ticket),operation))
    if operation=="identity":
        actor.update(name=name,description=description,personality=_label(args.get("personality"),limit=2000),
                     speaking_style=_label(args.get("voice"),limit=1000))
        label="Edit character · "+name
    elif operation=="player":
        selected=next((c for c in world["characters"] if c["id"]==args.get("player")),None)
        if not selected:
            raise ValueError("Select a saved character.")
        for c in world["characters"]: c["control"]="player" if c is selected else "npc"
        state["player"]=selected["id"]
        world["current_location_id"]=selected["location_id"]
        state["navigation"]={"player":[0,0],"camera":[0,0]}
        label="Play as "+selected["name"]
    elif operation=="rules":
        world["rules"]=[s.strip() for s in description.splitlines() if s.strip()]
        label="Revise world rules"
    elif operation=="character":
        world["characters"].append({"id":ident,"name":name,"description":description,"location_id":here})
        label="Introduce "+name
    elif operation=="objective":
        world["objectives"].append({"id":ident,"name":name,"description":description})
        label="Add objective · "+name
    elif operation in ("location","item"):
        row={"id":ident,"name":name,"description":description}
        if operation=="location": row["from_location_id"]=here
        else: row.update(location_id=here,kind="item")
        world=engine.apply_discoveries(world,{"locations" if operation=="location" else "entities":[row]},state["player"])
        label="Add "+name
    else:
        raise ValueError("Choose a supported world change.")
    state["world"]=engine.validate_world(world)
    store["turns"][ticket]={"parent":args["turn"],"author":"Player","action":label,"scene":source["scene"],
        "actions":copy.deepcopy(source["actions"]),"state_changes":[label],"game_state":state,"world_edit":copy.deepcopy(args)}
    store["active_turn"]=ticket
    return {"turn":ticket}


def imported(source):
    world=source.get("world")
    if not world:
        return None
    world=engine.validate_world(world)
    actor=source.get("player_character_id") or next((c["id"] for c in world["characters"] if c["control"]=="player"),None)
    if not actor or not any(c["id"]==actor for c in world["characters"]):
        return None
    pos=(source.get("navigation") or {}).get("position")
    if not (isinstance(pos,list) and len(pos)==2 and all(type(x) is int for x in pos)):
        pos=[0,0]
    return {"world":world,"player":actor,"navigation":{"player":list(pos),"camera":[0,0]}}
