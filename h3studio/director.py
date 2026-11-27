"""Persist reviewed Film production boards and branching Game sessions in each portable project."""
from __future__ import annotations

import copy
import json
import uuid

FILM_SCHEMA = '{"title":"Film title","shots":[{"title":"Shot title","duration":5,"prompt":"Visual action, camera and audio."}]}'
GAME_SCHEMA = '{"scene":"Narrate the observable result.","actions":["Possible next action"],"state_changes":["Proposed consequence"]}'


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
        return {"scene":_text(data.get("scene"),"scene"),"actions":_strings(data.get("actions",[]),"actions"),
                "state_changes":_strings(data.get("state_changes",[]),"state changes")}
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


def _history(store, parent):
    history = []
    while parent:
        turn = store["turns"][parent]
        history.append({k:turn[k] for k in ("action","scene","state_changes")})
        parent = turn["parent"]
    return list(reversed(history))


def request(studio, project, args):
    brief = _text(args.get("prompt"),"brief or player action")
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
        prompt = ("Accepted history (oldest first): "+json.dumps(accepted,ensure_ascii=False)+
                  "\nPlayer action: "+brief+"\nPropose one next turn as JSON: "+GAME_SCHEMA)
    payload = {"url":studio.prefs["director_url"],"model":studio.prefs["director_model"],"mode":mode,
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
            except ValueError as e:
                review["error"] = str(e)
            store["reviews"][key] = review
            if intent["mode"] == "film" and "parsed" in review and not intent.get("board"):
                store["boards"][key] = copy.deepcopy(review["parsed"])
    return {"director_review":key,"mode":intent["mode"],"storyboard":result["storyboard"]}


def _index(board, value):
    if type(value) is not int or not 0 <= value < len(board["rows"]):
        raise ValueError("Select an existing storyboard shot.")
    return value


def approve(project,args):
    key = args["review"]
    with project.transaction() as doc:
        store = _store(doc)
        review = store["reviews"][key]
        intent = review["intent"]
        if "parsed" not in review:
            review["parsed"] = _decode(_text(args.get("correction"),"corrected Director response",250000),
                                       intent["mode"],intent.get("count"))
            review.pop("error",None)
        draft = review["parsed"]
        if intent["mode"] == "game":
            if key not in store["turns"]:
                store["turns"][key] = {**draft,"parent":intent.get("parent"),"action":intent["brief"],"review":key}
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
            row.update(**fields,version=intent["version"]+1,revision=key)
            if shot:
                shot.update(name=row["title"],duration=row["duration"],director_prompt=row["prompt"])
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


def action(studio,project,op,args):
    if op == "director":
        return request(studio,project,args)
    if op == "director_approve":
        return approve(project,args)
    if op == "director_render":
        return render(studio,project,args)
    with project.transaction() as doc:
        store = _store(doc)
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
