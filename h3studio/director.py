"""Persist reviewed Film production boards and branching Game sessions in each portable project."""
from __future__ import annotations

import copy
import json
import uuid
from . import game, game_story

FILM_SCHEMA = '{"title":"Film title","shots":[{"title":"Shot title","duration":5,"prompt":"Visual action, camera and audio."}]}'
GAME_SCHEMA = '{"scene":"Narrate the observable result.","actions":["Possible next action"],"state_changes":["Proposed consequence"],"outcome":"resolved or blocked","discoveries":{"locations":[],"entities":[]},"world_effects":[]}'


def _text(value, name, limit=16000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"Enter a valid {name} (up to {limit} characters).")
    return value.strip()


def _strings(value, name):
    if not isinstance(value, list) or len(value) > 12:
        raise ValueError(f"Use a list of up to twelve {name}.")
    return [_text(v, name, 1000) for v in value]


def _value(raw):
    stripped = raw.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        stripped = stripped.partition("\n")[2].rsplit("```", 1)[0].strip()
    return json.loads(stripped)


def _decode(raw, mode, count=None, legacy=False):
    try:
        data = _value(raw)
    except (ValueError, TypeError):
        if not legacy:
            raise ValueError("The response needs a valid JSON object. Review or correct the text below.") from None
        data = {"title":"Recovered storyboard","shots":[{"title":"Shot 1","duration":5,"prompt":raw}]} if mode == "film" else {"scene":raw,"actions":[],"state_changes":[]}
    if not isinstance(data, dict):
        raise ValueError("The Director response must be a JSON object.")
    if mode == "game":
        outcome=data.get("outcome","resolved")
        if outcome not in ("resolved","blocked"):
            raise ValueError("Set the Game outcome to resolved or blocked.")
        return {"scene":_text(data.get("scene"),"scene"),"actions":_strings(data.get("actions",[]),"actions"),
                "state_changes":_strings(data.get("state_changes",[]),"state changes"),"outcome":outcome,
                "discoveries":data.get("discoveries"),"world_effects":data.get("world_effects",[])}
    shots = data.get("shots")
    if not isinstance(shots, list) or not 1 <= len(shots) <= 12 or (count and len(shots) != count):
        raise ValueError(f"Review the storyboard. Expected {count or '1–12'} shots.")
    rows = []
    for row in shots:
        if not isinstance(row, dict) or type(row.get("duration")) is not int or not 3 <= row["duration"] <= 15:
            raise ValueError("Each shot needs a title, a prompt and a whole duration of 3–15 seconds.")
        rows.append({"title":_text(row.get("title"),"shot title",180),"prompt":_text(row.get("prompt"),"shot prompt"),
                     "duration":row["duration"],"shot":None,"renders":[]})
    return {"title":_text(data.get("title","Film storyboard"),"film title",180),"rows":rows}


def _store(state):
    return state.setdefault("director",{"reviews":{},"boards":{},"turns":{},"active_turn":None})


def seed_example(project, shots):
    """Bind the Tidal Relay starter shots to an authored board and playable opening."""
    prompts = [
        "At dusk the last light hits the coastal listening station. Salt wind moves across the aerials.",
        "A receiver beside the window wakes. Its meter ticks twice and the operator leans in.",
        "Beyond the breakwater a distant light answers the signal, then slips behind the waves.",
    ]
    if len(shots)!=len(prompts) or len(set(shots))!=len(prompts):
        raise ValueError("Choose the three Tidal Relay starter shots in order.")
    with project.transaction() as doc:
        data = _store(doc)
        board_id = str(uuid.uuid5(uuid.UUID(doc["id"]),"Tidal Relay example board"))
        turn_id = str(uuid.uuid5(uuid.UUID(doc["id"]),"Tidal Relay game opening"))
        if board_id not in data["boards"]:
            rows=[]
            for i,shot_id in enumerate(shots):
                shot=doc["shots"][shot_id]
                shot.update(director_board=board_id,director_row=i,director_prompt=shot.get("director_prompt") or prompts[i])
                rows.append({"title":shot["name"],"prompt":shot["director_prompt"],"duration":shot["duration"],"shot":shot_id,"renders":[]})
            data["boards"][board_id]={"title":"Tidal Relay","author":"Studio example","rows":rows}
        if turn_id not in data["turns"]:
            data["turns"][turn_id]={
                "parent":None,"author":"Studio example","action":"Take the night watch at the coastal relay",
                "scene":"The receiver clicks twice. A voice reads your station call sign, then falls silent. Beyond the breakwater, a weak light repeats the same rhythm.",
                "actions":["Answer with the station call sign","Trace the signal on the receiver","Walk out to the breakwater"],
                "state_changes":["The station log contains an unidentified transmission."],
            }
            if not data["active_turn"]: data["active_turn"]=turn_id
        data["turns"][turn_id].setdefault("reference_shot",shots[0])
        if "game_state" not in data["turns"][turn_id]:
            data["turns"][turn_id]["game_state"]=game.initial(relay=True)
        game_story.seed(doc,data)
        return {"board":board_id,"turn":turn_id}


def _history(store, parent):
    history = []
    while parent:
        turn = store["turns"][parent]
        history.append({k:turn[k] for k in ("action","scene","state_changes")})
        parent = turn["parent"]
    return list(reversed(history))


def request(studio, project, args):
    brief = _text(args.get("prompt") or ("Player action" if args.get("game_intent") else ""),"brief or player action")
    mode = args.get("mode")
    if mode not in ("film","game"):
        raise ValueError("Choose Film or Game.")
    doc = project.state()
    store = _store(doc)
    intent = {"brief":brief,"mode":mode}
    if mode == "film":
        count = args.get("count",2)
        if type(count) is not int or not 1 <= count <= 12:
            raise ValueError("Choose one to twelve shots.")
        intent["count"] = count
        if board_id := args.get("board"):
            board = store["boards"][board_id]
            index = _index(board,args["row"])
            row = board["rows"][index]
            if row["shot"] and doc["shots"][row["shot"]].get("locked_take"):
                raise ValueError("Unlock this take before requesting a revision.")
            intent.update(board=board_id,row=index,count=1,version=row.get("version",0))
            context = [{"title":r["title"],"prompt":r["prompt"]} for r in board["rows"]]
            instruction = f"Revise only shot {index+1} of this saved storyboard. Context: {json.dumps(context,ensure_ascii=False)}"
        else:
            instruction = "Plan the following film as distinct renderable shots."
        prompt = (f"{instruction}\nUser brief: {brief}\nReturn exactly {intent['count']} shots as JSON. "
                  f"Use a whole-number duration from 3 through 15 seconds for each shot. Schema: {FILM_SCHEMA}")
    else:
        parent = args.get("parent")
        if parent and parent not in store["turns"]:
            raise ValueError("Resume an accepted Game turn.")
        intent["parent"] = parent
        accepted = _history(store,parent)
        brief,world_info,context=game.propose(store["turns"].get(parent),args,brief)
        intent.update(brief=brief,**world_info)
        prompt = ("Accepted history (oldest first): "+json.dumps(accepted,ensure_ascii=False)+"\n"+context+
                  "\nPlayer action: "+brief+"\nPropose one next turn as JSON. Set outcome to blocked if the player cannot complete the action. "
                  "Only propose new places and objects visibly established by the scene. Each discovery needs a new id and name. "
                  "A connected place may include from_location_id. An object may include location_id. "
                  "For a free-form action you may propose world_effects using only visible, named IDs, for example "
                  "{\"kind\":\"holder\",\"entity_id\":\"item-id\",\"character_id\":\"player-id\"}. "
                  "For a mechanical action use its supplied effects. Schema: "+GAME_SCHEMA)
    style=studio.prefs.get(mode+"_director_instructions","")
    if style: prompt+="\n"+mode.title()+" Director profile: "+style
    payload = {"url":studio.prefs.get(mode+"_director_url") or studio.prefs["director_url"],
               "model":studio.prefs.get(mode+"_director_model") or studio.prefs["director_model"],"mode":mode,
               "prompt":prompt,"studio_project":doc["id"],"director_request":intent}
    return studio.queue.enqueue("director",payload,job_id=args.get("request_id"))


def adopt(project,job):
    pl = job["payload"]
    result = job["receipt"]["result"]
    intent = pl.get("director_request") or {"mode":pl["mode"],"brief":pl["prompt"]}
    if (result.get("model") != pl["model"] or result.get("mode") != intent["mode"]
            or project.state()["id"] != pl["studio_project"] or job["status"] != "succeeded"):
        raise ValueError("The Director result differs from the selected project, mode or model.")
    key = job["id"]
    with project.transaction() as doc:
        store = _store(doc)
        if key not in store["reviews"]:
            raw = _text(result["storyboard"],"Director response",250000)
            review = {"id":key,"model":result["model"],"raw":raw,"intent":intent,"mode":intent["mode"]}
            try:
                review["parsed"] = _decode(raw,intent["mode"],intent.get("count"),legacy="director_request" not in pl)
                if intent["mode"]=="game":
                    game.outcome(store["turns"].get(intent.get("parent")),intent,review["parsed"],key)
            except ValueError as e:
                review.pop("parsed",None)
                review["error"] = str(e)
            store["reviews"][key] = review
            if intent["mode"] == "film" and "parsed" in review and not intent.get("board"):
                store["boards"][key] = copy.deepcopy(review["parsed"])
    return {"director_review":key,"mode":intent["mode"],"storyboard":result["storyboard"]}


def _index(board, value):
    if type(value) is not int or not 0 <= value < len(board["rows"]):
        raise ValueError("Select an existing storyboard shot.")
    return value


def _edit_row(row, shot, *, title, prompt, duration):
    if type(duration) is not int or not 3 <= duration <= 15:
        raise ValueError("Choose a whole duration of 3–15 seconds.")
    if shot and any(g["frame"] >= 24 * duration for g in shot["guides"]):
        raise ValueError("Move or remove guides beyond the new shot ending first.")
    row.update(title=_text(title,"shot title",180),prompt=_text(prompt,"shot prompt"),
               duration=duration,version=row.get("version",0)+1)
    if shot:
        shot.update(name=row["title"],duration=duration,director_prompt=row["prompt"])


def approve(project,args):
    key = args["review"]
    with project.transaction() as doc:
        store = _store(doc)
        if key in store["boards"] and key not in store["reviews"] and store["boards"][key].get("author"):
            # All starter rows are bound to existing shots. Authorship has no model receipt.
            return {"board":key}
        review = store["reviews"][key]
        intent = review["intent"]
        if "parsed" not in review:
            review["parsed"] = _decode(_text(args.get("correction"),"corrected Director response",250000),
                                       intent["mode"],intent.get("count"))
            review.pop("error",None)
        draft = review["parsed"]
        if intent["mode"] == "game":
            if key not in store["turns"]:
                saved=game.outcome(store["turns"].get(intent.get("parent")),intent,draft,key)
                store["turns"][key] = {**draft,"parent":intent.get("parent"),"action":intent["brief"],"review":key,"game_state":saved}
                if parent:=intent.get("parent"):
                    store["turns"][key]["scene_visual"]=game_story.visual(doc,store["turns"][parent])
                    store["turns"][key]["inherited_visual"]=True
                if intent.get("resolved"):
                    store["turns"][key]["resolved_action"]={**intent["resolved"],"action":intent["brief"]}
                store["active_turn"] = key
            return {"turn":key}
        if intent.get("board"):
            board = store["boards"][intent["board"]]
            row = board["rows"][intent["row"]]
            if row.get("revision") == key:
                return {"board":intent["board"]}
            shot = doc["shots"].get(row["shot"])
            if (shot and shot.get("locked_take")) or row.get("version",0) != intent["version"]:
                raise ValueError("This shot is locked or has changed since the revision request.")
            fields = {k:draft["rows"][0][k] for k in ("title","prompt","duration")}
            _edit_row(row,shot,**fields)
            row["revision"] = key
            return {"board":intent["board"]}
        board = store["boards"].setdefault(key,copy.deepcopy(draft))
        indices = [_index(board,args["row"])] if "row" in args else range(len(board["rows"]))
        for i in indices:
            row = board["rows"][i]
            if row["shot"]:
                continue
            shot = project._shot(row["title"],row["duration"])
            shot.update(director_board=key,director_row=i,director_prompt=row["prompt"])
            doc["shots"][shot["id"]] = shot
            # Per-shot approvals can arrive out of order. Keep the board's own
            # shots in storyboard order without rearranging unrelated cuts.
            following = next((r["shot"] for r in board["rows"][i+1:] if r["shot"] in doc["cut"]),None)
            preceding = next((r["shot"] for r in reversed(board["rows"][:i]) if r["shot"] in doc["cut"]),None)
            at = doc["cut"].index(following) if following else (doc["cut"].index(preceding)+1 if preceding else len(doc["cut"]))
            doc["cut"].insert(at,shot["id"])
            row["shot"] = shot["id"]
        return {"board":key}


def render(studio,project,args):
    from .integrations import _video, engine_action
    from .recipes import effective
    board_id = args["board"]
    ticket = str(uuid.UUID(args["request_id"]))
    original = project.state()
    board = _store(original)["boards"][board_id]
    indices = [_index(board,args["row"])] if "row" in args else range(len(board["rows"]))
    jobs = []
    for i in indices:
        key = uuid.uuid5(uuid.UUID(ticket),f"{board_id}/{i}").hex
        doc = project.state()
        row = _store(doc)["boards"][board_id]["rows"][i]
        shot = doc["shots"].get(row["shot"])
        if not shot or shot.get("locked_take"):
            if "row" in args:
                raise ValueError("Approve this shot and unlock its selected take before rendering.")
            continue
        history = row["renders"]
        matching = next((r for r in history if r["plan"] == key),None)
        if not matching:
            if history and history[-1]["version"] == row.get("version",0):
                if not args.get("retry"):
                    continue
                if "row" not in args or args.get("previous") != history[-1]["plan"]:
                    raise ValueError("The shot has a newer render request. Reopen the storyboard.")
            recipe = effective(doc, shot)
            seed = uuid.UUID(key).int % 2**32 if history else recipe.get("seed", uuid.UUID(key).int % 2**32)
            settings = {"resolution":"0.3", "quality":"fast", "save_mmh3":True, **recipe["settings"]}
            a = {"shot":shot["id"], "prompt":row["prompt"], "mode":recipe.get("mode","t2va"),
                 "seed":seed, "settings":settings}
            bundle,guides = _video(studio,project,a,doc,shot)
            with project.transaction() as current:
                latest = _store(current)["boards"][board_id]["rows"][i]
                if (current["shots"][shot["id"]].get("locked_take") or latest != row):
                    raise ValueError("The storyboard changed. Reopen it before rendering.")
                current.setdefault("prepared",{})[key] = {"shot":shot["id"],"guides":guides,"bundle":bundle,"prompt_text":row["prompt"],"request":a}
                latest["renders"].append({"plan":key,"version":row.get("version",0),"seed":seed})
        jobs.append(engine_action(studio,project,"submit_prepared",{"plan":key})["id"])
    return {"jobs":jobs}


def game_render(studio,project,args):
    """Render the accepted scene using its portable shot, guides and chosen H3 mode."""
    from .integrations import _video, engine_action
    from .recipes import effective
    turn_id = args["turn"]
    ident = uuid.UUID(args["request_id"]).hex
    shot_id = action(studio,project,"director_game_shot",{"turn":turn_id})["shot"]
    doc = project.state()
    turn = doc["director"]["turns"][turn_id]
    shot = doc["shots"][shot_id]
    if shot.get("locked_take"):
        raise ValueError("Unlock this scene's take before rendering.")
    if ident not in turn.get("renders",[]):
        recipe = effective(doc,shot)
        request = {"shot":shot_id,"prompt":game.visual_prompt(turn),"mode":args.get("mode") or recipe.get("mode","t2va"),
                   "seed":uuid.UUID(ident).int%2**32 if turn.get("renders") else recipe.get("seed",uuid.UUID(ident).int%2**32),
                   "settings":{"save_mmh3":True,**recipe["settings"]}}
        bundle,guides = _video(studio,project,request,doc,shot)
        with project.transaction() as state:
            current = state["director"]["turns"][turn_id]
            if state["shots"][shot_id]!=shot:
                raise ValueError("This scene's guides or selected take changed. Open the current scene.")
            if ident not in current.setdefault("renders",[]):
                if ident in state.setdefault("prepared",{}):
                    raise ValueError("This render identifier already belongs to another shot.")
                state["prepared"][ident]={"shot":shot_id,"bundle":bundle,"guides":guides,"request":request,"prompt_text":request["prompt"]}
                current["renders"].append(ident)
    return engine_action(studio,project,"submit_prepared",{"plan":ident})


def _recovered_game(doc, store, story_id, branch):
    """Materialize a recovered branch as portable turns, retaining each original run and take."""
    legacy = doc.get("legacy",{})
    story = legacy.get("documents",{}).get(f"stories/{story_id}.json",{}).get("document",{})
    key = f"story:{story_id}:{branch}"
    if story.get("id")!=story_id or story.get("mode")!="game" or key not in legacy.get("cuts",{}):
        raise ValueError("Select an imported Game session and one of its saved branches.")
    previous = str(uuid.uuid5(uuid.UUID(doc["id"]),f"legacy-game/{story_id}"))
    turns = store["turns"]
    turns.setdefault(previous,{"parent":None,"author":"Imported Game","legacy_story":story_id,
        "action":story.get("title") or story_id,"scene":story.get("premise") or "Recovered Game opening.",
        "actions":[],"state_changes":[]})
    by_run = {t["run_id"]:t for t in story.get("turns",[]) if t.get("run_id")}
    for entry in legacy["cuts"][key]:
        run = entry["legacy_run"]
        old = by_run.get(run,{})
        record = legacy["documents"].get("video_runs/"+run,{}).get("document",{})
        plan = old.get("plan") or {}
        scene = plan.get("action") or plan.get("setting") or record.get("prompt")
        if not isinstance(scene,str) or not scene.strip():
            raise ValueError("Restore the saved story plan or video run before continuing this branch. Missing scene for "+run)
        ident = str(uuid.uuid5(uuid.UUID(previous),run))
        lines = [scene] + [str(d.get("speaker",""))+": "+d["text"] for d in plan.get("dialogue",[]) if isinstance(d,dict) and d.get("text")]
        turns.setdefault(ident,{"parent":previous,"author":"Imported Game","legacy_story":story_id,"legacy_run":run,
             "source_take":entry["take"],"action":old.get("message") or plan.get("action") or run,
             "scene":"\n".join(lines),"actions":[v["message"] for v in plan.get("choices",[]) if isinstance(v,dict) and v.get("message")],
             "state_changes":[plan["final_state"]] if plan.get("final_state") else []})
        archive=(story.get("state_by_run") or {}).get(run) or old.get("accepted_state")
        if not archive and run==story.get("active_run_id") and branch==story.get("active_branch_id"):
            archive=story
        if isinstance(archive,dict) and "game_state" not in turns[ident]:
            if snapshot:=game.imported(archive):
                turns[ident]["game_state"]=snapshot
        previous = ident
    store["active_turn"] = previous
    return {"turn":previous}


def action(studio,project,op,args):
    if args.get("project_id") and (args["project_id"]!=project.state()["id"] or
           args.get("project_folder",str(project.folder))!=str(project.folder)):
        raise ValueError("The active project changed. Open the original Game or Film project.")
    if op.startswith("director_story_"):
        return game_story.action(project,op,args)
    if op == "director_world":
        turn=_store(project.state())["turns"][args["turn"]]
        return game.view(game.snapshot(turn))
    if op == "director":
        return request(studio,project,args)
    if op == "director_approve":
        return approve(project,args)
    if op == "director_render":
        return render(studio,project,args)
    if op == "director_game_render":
        return game_render(studio,project,args)
    if op == "director_inbox":
        if args["mode"] not in ("film","game"):
            raise ValueError("Choose Film or Game.")
        return studio.queue.director_inbox(project.state()["id"], args["mode"])
    with project.transaction() as doc:
        store = _store(doc)
        if op == "director_world_edit":
            return game.edit(store,args)
        if op == "director_resume_import":
            return _recovered_game(doc,store,args["story"],args["branch"])
        if op == "director_edit":
            board = store["boards"][args["board"]]
            row = board["rows"][_index(board,args["row"])]
            shot = doc["shots"].get(row["shot"])
            if (shot and shot.get("locked_take")) or row.get("version",0)!=args["version"]:
                raise ValueError("This shot is locked or has changed. Reopen the current storyboard.")
            _edit_row(row,shot,title=args.get("title"),prompt=args.get("prompt"),duration=args.get("duration"))
            return {"board":args["board"]}
        if op == "director_game_shot":
            turn = store["turns"][args["turn"]]
            if not turn.get("shot"):
                shot = project._shot(turn["action"][:180],5)
                shot.update(game_turn=args["turn"],director_prompt=game.visual_prompt(turn))
                frame=game_story.visual(doc,turn)
                if frame["revision"]: shot["guides"]=[dict(role="image",frame=0,usage="both",revision=frame["revision"])]
                elif frame["poster"]: shot["guides"]=[dict(role="image",frame=0,usage="both",asset=frame["poster"])]
                doc["shots"][shot["id"]] = shot
                doc["cut"].append(shot["id"])
                turn["shot"] = shot["id"]
            return {"shot":turn["shot"]}
        if op == "director_resume":
            if args["turn"] not in store["turns"]:
                raise ValueError("Choose an accepted Game turn.")
            store["active_turn"] = args["turn"]
            return {"turn":args["turn"]}
        if op == "director_lock":
            board = store["boards"][args["board"]]
            row = board["rows"][_index(board,args["row"])]
            shot = doc["shots"].get(row["shot"])
            if not shot or not shot["selected_take"] or shot["selected_take"] != args["take"]:
                raise ValueError("Open the current selected take before locking or unlocking it.")
            if type(args["locked"]) is not bool:
                raise ValueError("Choose lock or unlock.")
            shot["locked_take"] = shot["selected_take"] if args["locked"] else None
            return {"take":shot["locked_take"]}
    raise ValueError("Unknown Director action.")
