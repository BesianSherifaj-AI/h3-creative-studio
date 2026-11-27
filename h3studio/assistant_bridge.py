"""Project-bound pairing and creative tools for Studio's stdio assistant connection."""
from collections import deque
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sys
import time
import uuid

from . import cut, director, game, game_story, recipes


class UnknownTool(ValueError):
    pass


def obj(fields, required=()):
    return {"type": "object", "properties": fields, "required": list(required), "additionalProperties": False}


def string(limit, **extra):
    return {"type": "string", "minLength": 1, "maxLength": limit, **extra}


def integer(lo, hi):
    return {"type": "integer", "minimum": lo, "maximum": hi}


def array(items, lo, hi):
    return {"type": "array", "items": items, "minItems": lo, "maxItems": hi}


def tool(name, description, schema, *, proposal=False):
    return {"name": name, "description": description, "inputSchema": schema,
            "annotations": {"readOnlyHint": not proposal, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}}


ID = string(128)
PAGE = {"offset": integer(0, 1000000), "limit": integer(1, 100)}
KEY = string(100, description="Your stable request key. Reuse it only when retrying identical input.")
ROW = obj({"title": string(180), "prompt": string(6000), "duration": integer(3, 15)}, ("title", "prompt", "duration"))
GUIDE = obj({"revision": ID, "frame": integer(0, 2073599)}, ("revision", "frame"))
CONTROL = obj({"kind": string(60), "target_id": ID, "recipient_id": ID}, ("kind",))
GAME_SETUP = obj({"name": string(180), "location": string(180), "description": string(2400)}, ("name", "location"))
TOOLS = {t["name"]: t for t in [
    tool("project_overview", "Read the open project and ordered shots. Use view=timeline for Edit trim bounds, delivery frames, soundtrack routing and placed narration.", obj({
        **PAGE, "view": string(12, enum=["outline", "timeline"])})),
    tool("image_library", "List saved Image composition revisions, dimensions and layer counts. Use a revision ID as a shot guide.", obj(PAGE)),
    tool("shot_inspect", "Read a shot's effective editable prompt, selected take, guide frames and branch continuity.", obj({"shot": ID}, ("shot",))),
    tool("film_boards", "List Film storyboards, or inspect the shots, prompts and selected-take locks of one board.", obj({"board": ID, **PAGE})),
    tool("game_branches", "List accepted Game turns with parent IDs and available story choices.", obj(PAGE)),
    tool("game_world", "Read the active or specified turn, player, position, inventory, grounded world actions and revision.", obj({"turn": ID})),
    tool("proposal_status", "Check a proposal by ID, including approval in Film or Game.", obj({"proposal": ID}, ("proposal",))),
    tool("propose_film", "Send a storyboard to the author's Film inbox. Approval creates editable shots.", obj({
        "request_id": KEY, "title": string(180), "shots": array(ROW, 1, 12)}, ("request_id", "title", "shots")), proposal=True),
    tool("propose_game_turn", "Draft a Game child with parent and world_revision, or an independent opening with setup. Use game_world for enabled controls. The author accepts in Game.", obj({
        "request_id": KEY, "parent": ID, "world_revision": string(64), "action": string(1000), "control": CONTROL,
        "setup": GAME_SETUP,
        "scene": string(6000), "choices": array(string(500), 1, 8), "outcome": string(8, enum=["resolved", "blocked"])},
        ("request_id", "action", "scene", "choices")), proposal=True),
    tool("propose_shot_draft", "Suggest a new editable Motion prompt and optionally a saved Image revision as a timed guide. The author reviews before applying.", obj({
        "request_id": KEY, "shot": ID, "prompt": string(6000), "guide": GUIDE}, ("request_id", "shot", "prompt")), proposal=True),
    tool("propose_game_choice", "Suggest playing an authored choice from game_world.story.choices. The author reviews it before the branch becomes active.", obj({
        "request_id": KEY, "turn": ID, "choice": ID}, ("request_id", "turn", "choice")), proposal=True),
    tool("propose_voice_script", "Send an editable narration or dialogue script to the author for casting in Voice. Optionally attach the script to a shot or Game turn.", obj({
        "request_id": KEY, "title": string(120), "text": string(6000), "speaker": string(100), "shot": ID, "turn": ID},
        ("request_id", "title", "text", "speaker")), proposal=True),
]}


def validate(value, schema, label="arguments"):
    kind = schema["type"]
    if kind == "object":
        if not isinstance(value, dict) or set(value) - schema["properties"].keys() or set(schema["required"]) - value.keys():
            raise ValueError(f"Check the required and allowed fields in {label}.")
        for key, item in value.items():
            validate(item, schema["properties"][key], key)
    elif kind == "array":
        if not isinstance(value, list) or not schema["minItems"] <= len(value) <= schema["maxItems"]:
            raise ValueError(f"Check the number of items in {label}.")
        for item in value:
            validate(item, schema["items"], label)
    elif kind == "integer":
        if type(value) is not int or not schema["minimum"] <= value <= schema["maximum"]:
            raise ValueError(f"Enter a whole number in range for {label}.")
    elif not isinstance(value, str) or not value.strip() or len(value) > schema["maxLength"] or ("enum" in schema and value not in schema["enum"]):
        raise ValueError(f"Enter valid text for {label}.")


def page(rows, args):
    offset = args.get("offset", 0)
    end = offset + args.get("limit", 40)
    return {"items": rows[offset:end], "total": len(rows), "next_offset": end if end < len(rows) else None}


def lookup(records, key, label):
    if key not in records:
        raise ValueError(f"Choose an existing {label} in this project.")
    return records[key]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def shot_summary(shot, clip, *, in_cut=True):
    frames = clip["end_frame"] - clip["start_frame"]
    seconds = frames / cut.RATE
    return {**{k: shot.get(k) for k in ("id", "name", "selected_take", "locked_take", "needs_review")},
            "duration": seconds, "delivered_duration": seconds, "nominal_duration": shot["duration"],
            "frames": frames, "start_frame": clip["start_frame"] if in_cut else None,
            "end_frame": clip["end_frame"] if in_cut else None, "in_cut": in_cut,
            "source_in": clip["source_in"], "source_out": clip["source_out"],
            "media_ready": bool(clip["asset"]), "timebase": cut.RATE}


def draft_signature(doc, shot):
    return digest([recipes.effective(doc, shot), shot["guides"], shot["duration"], shot.get("selected_take"), shot.get("locked_take"),
                   doc.get("recipe_drafts", {}).get(shot["id"], {}).get("start")])


def story_view(doc, store, turn):
    game_story.seed(doc, store)
    return game_story._view(doc, store, game_story._lineage(store, turn))


def timeline(doc, edit, args):
    fields = ("shot", "take", "fps", "audio", "locked")
    clips = [shot_summary(doc["shots"][c["shot"]], c) | {k: c.get(k) for k in fields} for c in edit["clips"]]
    bed_fields = ("id", "name", "offset", "gain_db", "track", "stream", "asset")
    beds = [{k: b.get(k) for k in bed_fields} for b in edit["beds"]]
    voice = [{k: c.get(k) for k in ("id", "title", "text", "speaker", "source", "duration")}
             | {"placed": "voice-" + c["id"] in doc.get("soundtracks", {})} for c in doc.get("narrations", {}).values()]
    return {"project": doc["id"], "name": doc["name"], "frames": edit["frames"], "fps": edit["fps"],
            "cut": page(clips, args), "soundtracks": page(beds, args), "narrations": page(voice, args)}


def status(doc, key, record):
    store = doc.get("director", {})
    if record["kind"] == "propose_film" and key in store.get("boards", {}):
        return "approved"
    if record["kind"] == "propose_game_turn" and key in store.get("turns", {}):
        return "approved"
    return record["status"]


def inspect(project, doc, name, args):
    store = doc.get("director", {})
    turns, boards = store.get("turns", {}), store.get("boards", {})
    if name == "project_overview":
        edit = cut._view(project, doc)
        if args.get("view") == "timeline":
            return timeline(doc, edit, args)
        return {"project": doc["id"], "name": doc["name"], "active_game_turn": store.get("active_turn"),
                "images": len(doc["revisions"]), "film_boards": len(boards), "game_turns": len(turns),
                "shot_timebase": edit["fps"], "frames": edit["frames"],
                "cut": page([shot_summary(doc["shots"][c["shot"]], c) for c in edit["clips"]], args)}
    if name == "image_library":
        return page([{"id": r["id"], "name": r["name"], "width": r["width"], "height": r["height"], "layers": len(r["layers"]),
                      "composition": r["composition"], "current": doc["compositions"][r["composition"]]["current"] == r["id"]}
                     for r in reversed(list(doc["revisions"].values()))], args)
    if name == "shot_inspect":
        shot = lookup(doc["shots"], args["shot"], "shot")
        effective = recipes.effective(doc, shot)
        active = shot["id"] in doc["cut"]
        edit = cut._view(project, doc if active else {**doc, "cut": [shot["id"]]})
        clip = next(c for c in edit["clips"] if c["shot"] == shot["id"])
        return {**shot_summary(shot, clip, in_cut=active), "prompt": effective.get("prompt", ""), "mode": effective.get("mode"),
                "seed": effective.get("seed"), "parent_take": shot.get("parent_take"), "takes": shot["takes"],
                "guides": [{k: g[k] for k in ("frame", "role", "usage", "asset", "revision", "audio_asset") if k in g} for g in shot["guides"]]}
    if name == "film_boards":
        if "board" in args:
            b = lookup(boards, args["board"], "board")
            return {"id": args["board"], "title": b["title"], "rows": page([
                {k: r.get(k) for k in ("title", "duration", "prompt", "shot")} | {"locked_take": doc["shots"].get(r["shot"], {}).get("locked_take")}
                for r in b["rows"]], args)}
        return page([{"id": i, "title": b["title"], "rows": len(b["rows"])} for i, b in boards.items()], args)
    if name == "game_branches":
        return {"active": store.get("active_turn"), **page([{"id": i, "parent": t.get("parent"), "action": t["action"],
                        "choices": t.get("actions", []), "shot": t.get("shot")} for i, t in turns.items()], args)}
    if name == "game_world":
        ident = args.get("turn", store.get("active_turn"))
        t = lookup(turns, ident, "Game turn")
        story = story_view(doc, store, ident)
        return {"turn": ident, "parent": t.get("parent"), "scene": t["scene"], "choices": t.get("actions", []),
                "story": {k: story[k] for k in ("node", "ending", "choices", "visual")}, **game.view(game.snapshot(t))}
    r = lookup(doc.get("assistant_proposals", {}), args["proposal"], "proposal")
    state = status(doc, args["proposal"], r)
    result = r.get("result")
    if state == "approved" and r["kind"] in ("propose_film", "propose_game_turn"):
        result = {"board" if r["kind"] == "propose_film" else "turn": args["proposal"]}
    return {"id": args["proposal"], "kind": r["kind"], "status": state, "title": r["title"], "result": result}


def propose(project, kind, args):
    with project.transaction() as doc:
        records = doc.setdefault("assistant_proposals", {})
        key = str(uuid.uuid5(uuid.UUID(doc["id"]), "h3-assistant:" + args["request_id"]))
        signature = digest([kind, args])
        if key in records:
            if records[key]["input_hash"] != signature:
                raise ValueError("That request ID belongs to a different proposal.")
            return inspect(project, doc, "proposal_status", {"proposal": key})
        if sum(status(doc, i, r) == "pending" for i, r in records.items()) >= 100:
            raise ValueError("Review pending AI proposals before adding more.")
        record = {"kind": kind, "status": "pending", "input_hash": signature}
        if kind == "propose_shot_draft":
            shot = lookup(doc["shots"], args["shot"], "shot")
            if shot.get("locked_take"):
                raise ValueError("Unlock this shot in Film before proposing a draft.")
            if g := args.get("guide"):
                lookup(doc["revisions"], g["revision"], "Image revision")
                if g["frame"] >= round(24 * shot["duration"]):
                    raise ValueError("Place the guide within the delivered shot.")
            record.update(title=shot["name"], shot=shot["id"], prompt=args["prompt"], guide=args.get("guide"), base=draft_signature(doc, shot))
        elif kind == "propose_voice_script":
            if args.get("shot"):
                lookup(doc["shots"], args["shot"], "shot")
            if args.get("turn"):
                lookup(doc.get("director", {}).get("turns", {}), args["turn"], "Game turn")
            record.update(title=args["title"], script={k: v for k, v in args.items() if k != "request_id"})
        elif kind == "propose_game_choice":
            store = director._store(doc)
            choices = story_view(doc, store, args["turn"])["choices"]
            choice = lookup({c["id"]: c for c in choices}, args["choice"], "story choice")
            record.update(title=choice["label"], turn=args["turn"], choice=choice["id"])
        else:
            store = director._store(doc)
            if kind == "propose_game_turn":
                parent = args.get("parent")
                if bool(parent) != bool(args.get("world_revision")) or bool(parent) == bool(args.get("setup")):
                    raise ValueError("Choose a parent and its world revision, or provide setup for a new opening.")
                t = lookup(store["turns"], parent, "Game turn") if parent else None
                movement = {"world_revision": args.get("world_revision"), "game_setup": args.get("setup"), "game_intent": args.get("control")}
                brief, world, _ = game.propose(t, movement, args["action"])
                intent = {"mode": "game", "brief": brief, "parent": parent, **world}
                raw = json.dumps({"scene": args["scene"], "actions": args["choices"], "outcome": args.get("outcome", "resolved")}, ensure_ascii=False)
            else:
                intent = {"mode": "film", "brief": args["title"], "count": len(args["shots"])}
                raw = json.dumps({"title": args["title"], "shots": args["shots"]}, ensure_ascii=False)
            draft = director._decode(raw, intent["mode"], intent.get("count"))
            if intent["mode"] == "game":
                game.outcome(t, intent, draft, key)
            store["reviews"][key] = {"id": key, "model": "Connected AI client", "mode": intent["mode"], "intent": intent, "raw": raw, "parsed": draft}
            record["title"] = intent["brief"]
        records[key] = record
        return inspect(project, doc, "proposal_status", {"proposal": key})


def decide(project, key, accept):
    with project.transaction() as doc:
        rec = lookup(doc.get("assistant_proposals", {}), key, "proposal")
        result = status(doc, key, rec)
        if result != "pending":
            return {"status": result, "result": rec.get("result")}
        if not accept:
            doc.get("director", {}).get("reviews", {}).pop(key, None)
            rec["status"] = "dismissed"
        elif rec["kind"] == "propose_voice_script":
            rec["status"] = "approved"
        elif rec["kind"] == "propose_game_choice":
            store = director._store(doc)
            game_story.seed(doc, store)
            rec["result"] = game_story._choose(doc, store, game_story._lineage(store, rec["turn"]), {"choice": rec["choice"]})
            rec["status"] = "approved"
        elif rec["kind"] == "propose_shot_draft":
            shot = lookup(doc["shots"], rec["shot"], "shot")
            if shot.get("locked_take") or draft_signature(doc, shot) != rec["base"]:
                raise ValueError("This shot changed since the proposal. Request a fresh draft.")
            entry = doc.setdefault("recipe_drafts", {}).setdefault(shot["id"], {})
            entry["recipe"] = recipes.recipe({**recipes.effective(doc, shot), "prompt": rec["prompt"]})
            if g := rec.get("guide"):
                lookup(doc["revisions"], g["revision"], "Image revision")
                guides = [old for old in shot["guides"] if (old["frame"], old.get("role", "image")) != (g["frame"], "image")]
                shot["guides"] = sorted([*guides, {**g, "role": "image", "usage": "timed"}], key=lambda item: item["frame"])
            rec["status"] = "approved"
            rec["result"] = {"shot": shot["id"]}
        else:
            raise ValueError("Review and approve this proposal in its Director room.")
        return {"status": rec["status"], "result": rec.get("result")}


class AssistantBridge:
    def __init__(self, server):
        self.server = server
        self.studio = server.studio
        self.project = None
        self.token = None
        self.expires = 0
        self.scope = "read"
        self.path = None
        self.last_seen = None
        self.recent_calls = deque()

    def revoke(self):
        self.project = self.token = None
        self.expires = 0
        self.last_seen = None
        self.recent_calls.clear()
        if self.path:
            try:
                self.path.unlink(missing_ok=True)
            except PermissionError:
                # Windows may briefly lock a file while a paired client reads it.
                # Revoking the in-memory bearer is authoritative.
                pass
            self.path = None

    def active(self):
        if self.token and (self.project is not self.studio.project or time.time() >= self.expires):
            self.revoke()
        return bool(self.token)

    def enable(self, scope):
        if scope not in ("read", "propose"):
            raise ValueError("Choose a supported client permission.")
        self.revoke()
        self.path = self.studio.home / "ai-connections" / (uuid.uuid4().hex + ".json")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        # The file lives in the user's application data, outside shared project media.
        with os.fdopen(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w", encoding="utf-8") as f:
            json.dump({"port": self.server.server_port, "token": token}, f)
        self.token, self.project, self.scope = token, self.studio.project, scope
        self.expires = time.time() + 8 * 3600

    def desktop(self, op, args):
        with self.studio.lock:
            if op == "assistant_enable":
                self.enable(args.get("scope", "read"))
            elif op == "assistant_revoke":
                self.revoke()
            elif op == "assistant_decide":
                if (args.get("project") != self.studio.project.state()["id"] or
                        ("project_folder" in args and args["project_folder"] != str(self.studio.project.folder))):
                    raise ValueError("Reopen AI clients for this project.")
                if args.get("decision") not in ("approve", "dismiss"):
                    raise ValueError("Choose approve or dismiss.")
                return decide(self.studio.project, args["proposal"], args["decision"] == "approve")
            elif op != "assistant_status":
                raise ValueError("Unknown client setup command.")
            active = self.active()
            doc = self.studio.project.state()
            python = Path(sys.executable)
            if python.stem.lower() == "pythonw":
                python = python.with_name("python.exe")
            config = {"command": str(python), "args": ["-I", str(Path(__file__).with_name("agent_cli.py").resolve()), "--session", str(self.path)]}
            pending = [{"id": i, **{k: r.get(k) for k in ("kind", "title", "prompt", "guide", "script", "turn")}}
                       for i, r in doc.get("assistant_proposals", {}).items() if status(doc, i, r) == "pending"]
            scripts = [r["script"] for r in doc.get("assistant_proposals", {}).values()
                       if r["kind"] == "propose_voice_script" and r["status"] == "approved"]
            return {"active": active, "scope": self.scope if active else "read", "expires": self.expires, "last_seen": self.last_seen,
                    "project": doc["id"], "folder": str(self.studio.project.folder), "name": doc["name"],
                    "config": {"mcpServers": {"h3-studio": config}} if active else None, "pending": pending, "scripts": scripts}

    def request(self, bearer, method, params):
        with self.studio.lock:
            if not self.active() or not hmac.compare_digest(bearer, self.token):
                raise PermissionError("Connection expired or revoked. Open AI clients in Studio to pair again.")
            now = time.monotonic()
            while self.recent_calls and self.recent_calls[0] <= now - 60:
                self.recent_calls.popleft()
            if len(self.recent_calls) >= 240:
                raise ValueError("This connection reached 240 calls in a minute. Wait one minute and retry.")
            self.recent_calls.append(now)
            self.last_seen = time.time()
            if method == "tools/list":
                return {"tools": [t for t in TOOLS.values() if self.scope == "propose" or t["annotations"]["readOnlyHint"]]}
            if method != "tools/call" or not isinstance(params, dict):
                raise ValueError("Unknown assistant command.")
            name = params.get("name")
            if not isinstance(name, str) or name not in TOOLS:
                raise UnknownTool("Unknown Studio tool. Use tools/list to discover available tools.")
            args = params.get("arguments", {})
            validate(args, TOOLS[name]["inputSchema"])
            if TOOLS[name]["annotations"]["readOnlyHint"]:
                doc = self.project.state()
                return inspect(self.project, doc, name, args)
            if self.scope != "propose":
                raise PermissionError("Enable reviewable drafts in Studio to propose work.")
            return propose(self.project, name, args)
