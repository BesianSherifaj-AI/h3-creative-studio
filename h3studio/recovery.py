"""Read installed H3 libraries and portable project ZIPs without modifying their sources."""
from __future__ import annotations

from contextlib import closing
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import zipfile

from .project import Project, packet_info


def _id(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _child(root, *parts):
    """Old documents can supply names, but the user controls the root directory."""
    root = Path(root).resolve()
    for part in parts:
        name = PurePosixPath(str(part).replace("\\", "/"))
        if name.is_absolute() or ".." in name.parts or ":" in str(name):
            raise ValueError("Invalid relative path in the legacy project.")
    path = root.joinpath(*parts).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Legacy data refers outside the selected library.")
    return path


def _json(path):
    if path.stat().st_size > 32 << 20:
        raise ValueError(f"Oversized legacy metadata: {path.name}")
    return json.loads(path.read_text("utf-8-sig"))


def _report(state):
    return {"compositions": len(state["compositions"]), "revisions": len(state["revisions"]),
            "shots": len(state["shots"]), "takes": len(state["takes"]), "assets": len(state["assets"]),
            "missing": list(state["missing"].values())}


class _Import:
    def __init__(self, project, state, media_root, cancelled=None):
        self.p = project
        self.s = state
        self.output = Path(media_root).resolve() if media_root else None
        self.catalog = state["legacy"].setdefault("documents", {})
        self.resolved = state["legacy"].setdefault("resolved", {})
        self.cuts = state["legacy"].setdefault("cuts", {})
        self.memo = {}
        self.notices = []
        self.cancelled = cancelled

    def check_cancelled(self):
        if self.cancelled and self.cancelled():
            raise InterruptedError("Legacy import cancelled.")

    def missing(self, key, name, kind="asset", sha=None):
        ident = "missing-" + _id(*key)
        if ident in self.resolved:
            return self.resolved[ident]
        self.s["missing"].setdefault(ident, {"id": ident, "name": str(name), "kind": kind,
                                              "sha256": sha, "source": list(key)})
        return ident

    def blob(self, key, name, *, path=None, archive=None, member=None, sha=None, kind="asset"):
        self.check_cancelled()
        token = "missing-" + _id(*key)
        if token in self.memo:
            return self.memo[token]
        stream = None
        if path is not None and path.is_file():
            stream = path.open("rb")
        elif archive and member in archive.namelist():
            stream = archive.open(member)
        if stream is None:
            return self.missing(key, name, kind, sha)
        with stream:
            options = {"cancelled":self.cancelled} if self.cancelled else {}
            record = self.p.store_stream(stream, name, expected_hash=sha, **options)
        self.s["assets"].setdefault(record["id"], record)
        self.resolved[token] = self.memo[token] = record["id"]
        self.s["missing"].pop(token, None)
        return record["id"]

    def atelier_revision(self, project_id, title, payload, stamp, history_id, asset):
        comp_id = _id("atelier", project_id)
        old = self.s["compositions"].setdefault(comp_id, {"id": comp_id, "revisions": [], "current": None})
        # A project row and its newest snapshot often describe the same edit.
        revision = _id("atelier-revision", project_id, title, payload, stamp)
        layers = copy.deepcopy(payload.get("layers", []))
        keys = {k: asset(payload[k]) for k in ("source", "original", "mask") if payload.get(k)}
        references = []
        for ref in payload.get("references", []):
            if isinstance(ref, dict) and ref.get("id"):
                references.append(dict(ref, id=asset(ref["id"])))
        for layer in layers:
            layer["asset"] = asset(layer["asset"])
            if layer.get("mask"):
                layer["mask"] = asset(layer["mask"])
        if not layers and keys.get("source"):
            layers = [{"asset": keys["source"], "name": title}]
        previous_latest = old["revisions"][-1] if old["revisions"] else None
        added = revision not in old["revisions"]
        if added:
            old["revisions"].append(revision)
        entry = self.s["revisions"].setdefault(revision, {})
        entry.update(id=revision, composition=comp_id, name=title, width=payload.get("width",1024),
                     height=payload.get("height",1024), layers=layers, updated=stamp, references=references,
                     source_fields=keys, form=payload.get("form", {}))
        if history_id:
            entry["legacy_revision_id"] = history_id
        # Re-importing an old backup never replaces the user's newer local edit.
        ordered = sorted((r for r in old["revisions"] if "updated" in self.s["revisions"][r]),
                         key=lambda r: self.s["revisions"][r]["updated"])
        newer = [r for r in old["revisions"] if r not in ordered]
        old["revisions"] = ordered + newer
        for i, r in enumerate(ordered):
            self.s["revisions"][r]["parent"] = ordered[i-1] if i else None
        if not old["current"] or (added and old["current"] == previous_latest):
            old["current"] = old["revisions"][-1]
        old["name"] = self.s["revisions"][old["current"]]["name"]

    def atelier_db(self, path):
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"projects","assets"} <= tables:
                raise ValueError("Choose an Atelier project database.")
            metadata = {r["id"]: dict(r) for r in db.execute("SELECT id,name,sha256 FROM assets")}
            def asset(ident):
                meta = metadata.get(ident, {})
                return self.blob(("atelier-asset",ident), meta.get("name",ident) + ".png",
                                 path=_child(path.parent,"assets",ident+".png"), sha=meta.get("sha256"))
            for row in db.execute("SELECT * FROM projects ORDER BY updated"):
                if "project_revisions" in tables:
                    for old in db.execute("SELECT * FROM project_revisions WHERE project=? ORDER BY created",(row["id"],)):
                        self.atelier_revision(row["id"],old["name"],json.loads(old["payload"]),old["created"],old["id"],asset)
                self.atelier_revision(row["id"],row["name"],json.loads(row["payload"]),row["updated"],None,asset)

    def prompt_project(self, doc, relative, asset, *, active):
        project_id = str(doc.get("id") or _id(relative))
        mapped = {}
        for a in doc.get("assets", []):
            if isinstance(a,dict) and a.get("id"):
                mapped[a["id"]] = asset(a)
        self.catalog[relative] = {"document":doc, "assets":mapped}
        order = []
        for i, source in enumerate(doc.get("shots", [])):
            if not isinstance(source,dict):
                continue
            historical = relative.startswith("history/")
            ident = (_id("project-version",relative,source.get("id",i)) if historical else
                     _id("prompt-shot",project_id,source.get("id",i)))
            order.append(ident)
            first_visit = ident not in self.s["shots"]
            if first_visit:
                name = source.get("title") or source.get("action") or f"{doc.get('title','Shot')} {i+1}"
                shot = Project._shot(name,source.get("duration") or doc.get("duration") or 5)
                shot.update(id=ident, legacy_project=project_id, legacy_shot=source.get("id"), workspace=doc.get("workspace","studio"),
                            settings={"mode":doc.get("mode","t2va"),"legacy_project":relative,"director_beat":source})
                for ref in doc.get("assets",[]):
                    if not ref.get("enabled",True) or ref.get("media_type") not in ("image","video","audio"):
                        continue
                    if ref["id"] not in mapped:
                        continue
                    role = ref.get("role","reference_image")
                    shot["guides"].append({"frame":round(shot["duration"]*24)-1 if role=="last_frame" else 0,
                                           "asset":mapped[ref["id"]],"role":ref["media_type"],"source_role":role})
                self.s["shots"][ident] = shot
            if active and first_visit:
                self.s["cut"].append(ident)
        if active:
            self.cuts["project:" + project_id] = order
        elif relative.startswith("history/"):
            self.cuts["version:" + relative] = order
        if doc.get("story_session_id"):
            self.missing(("story",doc["story_session_id"]),f"stories/{doc['story_session_id']}.json","story")
        return order

    def prompt_asset(self, root, entry):
        ident = entry["id"]
        folder = _child(root, "assets", ident)
        meta = _json(folder / "metadata.json") if (folder / "metadata.json").is_file() else entry
        name = meta.get("filename") or entry.get("filename") or "source"
        return self.blob(("prompt-asset", ident),name,path=_child(folder,name),sha=meta.get("sha256"))

    def story(self, doc, rel):
        self.catalog[rel] = {"document":doc}
        self.s["missing"].pop("missing-" + _id("story",doc["id"]),None)
        self.resolved["missing-"+_id("story",doc["id"])] = doc["id"]
        for name, chain in doc.get("branches", {}).items():
            self.cuts["story:" + doc["id"] + ":" + name] = [
                {"take":_id("prompt-run",run), "legacy_run":run} for run in chain]
            for run in chain:
                if _id("prompt-run",run) not in self.s["takes"]:
                    self.missing(("run",run),f"video_runs/{run}/record.json","run")

    def run(self, root, folder, record):
        run = record["id"]
        files = {}
        for name in ("request.json","project.json","transfer.json","ending-asset.json"):
            if (folder / name).is_file():
                files[name] = _json(folder / name)
        project = files.get("project.json",{})
        shot_order = self.prompt_project(project, "video_runs/"+run+"/project.json",
                                         lambda a:self.prompt_asset(root,a),active=False) if project else []
        if not shot_order:
            shot_order = self.cuts.get("project:"+str(record.get("project_id")),[])
        if not shot_order:
            shot_id = _id("orphan-shot",record.get("project_id") or run)
            if shot_id not in self.s["shots"]:
                shot = Project._shot(record.get("title","Recovered shot"),record.get("duration") or 5)
                shot["id"] = shot_id
                self.s["shots"][shot_id] = shot
            shot_order = [shot_id]
        # An H3 run renders a whole project, which may describe several internal director beats.
        shot_id = _id("rendered-project",record.get("project_id") or run) if len(shot_order)>1 else shot_order[0]
        if shot_id not in self.s["shots"]:
            shot = Project._shot(record.get("title","Recovered shot"),record.get("duration") or 5)
            self.s["shots"][shot_id] = dict(shot,id=shot_id,source_beats=shot_order)
        take_id = _id("prompt-run",run)
        previous = self.s["takes"].get(take_id,{}).get("settings",{})
        video = packet = info = file_revision = None
        rendered = record.get("video") or {}
        source = folder / "playback.mp4"
        if not source.is_file() and self.output and rendered.get("filename"):
            source = _child(self.output,rendered.get("subfolder",""),rendered["filename"])
        if rendered or source.is_file():
            video = self.blob(("run-video",run),rendered.get("filename") or "playback.mp4",path=source,kind="video")
        continuation = record.get("continuation_source")
        if continuation:
            external = _child(self.output,continuation) if self.output else None
            key = ("run-packet",run)
            token = "missing-" + _id(*key)
            try:
                # A previously recovered or relinked packet travels with the project.
                # The installed library can still contain the corrupt copy it replaced.
                packet = self.resolved.get(token)
                if packet not in self.s["assets"]:
                    packet = self.blob(key,Path(continuation).name,path=external,kind="packet",
                                       sha=record.get("packet_sha256"))
                    if external and external.is_file():
                        stat = external.stat()
                        file_revision = f"{stat.st_mtime_ns:x}-{stat.st_size:x}"
                if packet in self.s["assets"]:
                    info = packet_info(self.p._asset_path(self.s,packet),record)
            except ValueError as error:
                self.notices.append(f"{run}: {error}")
                self.resolved.pop(token,None)
                self.memo.pop(token,None)
                file_revision = None
                packet = self.missing(key,Path(continuation).name,"packet",record.get("packet_sha256"))
        manifest = files.get("transfer.json",{}).get("manifest",{})
        settings = dict(manifest)
        settings.update({k:record[k] for k in ("seed","frames","width","height","overlap_frames","packet_id") if k in record})
        transfer = files.get("transfer.json",{})
        settings.update(graph=transfer.get("prompt"),workflow=transfer.get("workflow"),
                        prompt=files.get("request.json",{}).get("prompt"),
                        motion_selector="output::"+continuation.removeprefix("output::") if continuation else None,
                        motion_revision=file_revision or record.get("motion_revision") or previous.get("motion_revision"),
                        original_kind=record.get("kind"),origin_source=record.get("continuation_input_source"))
        parent = record.get("parent_run_id")
        take = {"id":take_id,"shot":shot_id,"settings":settings,"guides":[],"video_asset":video,
                "packet_asset":packet,"packet":info,"parent_take":_id("prompt-run",parent) if parent else None,
                "trim":None,"duration":record.get("duration") or self.s["shots"][shot_id]["duration"],
                "legacy_run":run,"legacy_status":record.get("status")}
        if (folder / "ending.png").is_file():
            take["ending_asset"] = self.blob(("ending",run),"ending.png",path=folder/"ending.png")
        self.catalog["video_runs/"+run] = {"document":record, "files":files}
        self.s["takes"][take_id] = take
        shot = self.s["shots"][shot_id]
        if take_id not in shot["takes"]:
            shot["takes"].append(take_id)
            if record.get("status") == "succeeded":
                shot["selected_take"] = take_id
        self.s["missing"].pop("missing-"+_id("run",run),None)
        self.resolved["missing-"+_id("run",run)] = take_id

    def production_export(self, root, manifest_path):
        folder = manifest_path.parent
        batch,version = folder.parent.name,folder.name
        document = _json(manifest_path)
        cut = []
        entry = {"document":document,"assets":{}}
        self.catalog[manifest_path.relative_to(root).as_posix()] = entry
        for item in document.get("clips",[]):
            index = item["index"]
            file = _child(folder,item["file"])
            media = self.blob(("production-clip",batch,version,index),file.name,path=file,kind="video")
            entry["assets"][item["file"]] = media
            sh = _id("export-shot",batch,version,index)
            take_id = _id("export-take",batch,version,index)
            if sh not in self.s["shots"]:
                shot = Project._shot(item.get("title") or f"Shot {index}",item["duration"])
                self.s["shots"][sh] = dict(shot,id=sh,takes=[take_id],selected_take=take_id)
                self.s["takes"][take_id] = {"id":take_id,"shot":sh,"duration":item["duration"],
                    "video_asset":media,"packet_asset":None,"packet":None,"guides":[],
                    "settings":{"kind":"legacy delivery","edited_ending":True,"source_edit":item,
                                "export":manifest_path.relative_to(root).as_posix()},
                    "parent_take":_id("prompt-run",item["run_id"]) if item.get("run_id") else None,"trim":None}
            else:
                self.s["takes"][take_id]["video_asset"] = media
            cut.append({"take":take_id})
        if (folder/"film.mp4").is_file():
            entry["assets"]["film.mp4"] = self.blob(("production-film",batch,version),"film.mp4",path=folder/"film.mp4")
        self.cuts["export:"+batch+":"+version] = cut

    def prompt_library(self, root):
        for group, pattern in (("projects","*.json"),("history","*/*.json")):
            for path in sorted((root / group).glob(pattern)):
                self.prompt_project(_json(path),path.relative_to(root).as_posix(),lambda a:self.prompt_asset(root,a),active=group=="projects")
        for path in sorted((root / "films").glob("*.json")):
            doc = _json(path)
            self.catalog[path.relative_to(root).as_posix()] = {"document":doc}
        for path in sorted((root / "production").glob("*/*.json")):
            doc = _json(path)
            rel = path.relative_to(root).as_posix()
            if path.name.startswith(("project-", "image-project-", "video-project-")) and doc.get("shots"):
                self.prompt_project(doc,rel,lambda a:self.prompt_asset(root,a),active=False)
            else:
                self.catalog[rel] = {"document":doc}
                if path.name == "record.json":
                    self.cuts["production:"+doc["id"]] = [
                        {"take":_id("prompt-run",i["run_id"]),"legacy_run":i["run_id"]}
                        for i in doc.get("items",[]) if i.get("run_id")]
        runs = [(path, _json(path)) for path in (root / "video_runs").glob("*/record.json")]
        for path,record in sorted(runs,key=lambda item:(item[1].get("created_at",0),str(item[0]))):
            self.run(root,path.parent,record)
        for manifest in sorted((root/"production_exports").glob("*/*/manifest.json")):
            self.production_export(root,manifest)
        for key,catalog in self.catalog.items():
            if key.startswith("films/"):
                film = catalog["document"]
                current = self.cuts.get("production:"+str(film.get("latest_batch_id")))
                if current:
                    self.cuts["film:"+film["id"]] = current
        for story in sorted((root / "stories").glob("*.json")):
            self.story(_json(story),story.relative_to(root).as_posix())
        for old in sorted((root / "stories" / "originals").glob("*.json")):
            self.catalog[old.relative_to(root).as_posix()] = {"document":_json(old)}

    def portable_zip(self, path):
        with zipfile.ZipFile(path) as z:
            members = z.infolist()
            if (len(set(z.namelist())) != len(members) or len(members)>4096 or
                    sum(m.file_size for m in members)>512 << 20 or z.getinfo("project.json").file_size>8 << 20):
                raise ValueError("Invalid or oversized portable project ZIP.")
            doc = json.loads(z.read("project.json"))
            if "payload" in doc:
                def asset(ident):
                    return self.blob(("atelier-asset",ident),ident+".png",archive=z,member="assets/"+ident+".png")
                self.atelier_revision(doc.get("id") or _id(doc),doc.get("name","Imported canvas"),doc["payload"],
                                      doc.get("updated",0),None,asset)
            elif "shots" in doc:
                def asset(entry):
                    ident = entry["id"]
                    meta_path = f"assets/{ident}/metadata.json"
                    if meta_path in z.namelist() and z.getinfo(meta_path).file_size <= 1<<20:
                        meta = json.loads(z.read(meta_path))
                    else:
                        meta = entry
                    name = meta.get("filename") or "source"
                    if Path(name).name != name or "/" in name or "\\" in name:
                        raise ValueError("Invalid portable asset filename.")
                    return self.blob(("prompt-asset",ident),name,archive=z,member=f"assets/{ident}/{name}",sha=meta.get("sha256"))
                self.prompt_project(doc,"portable/"+str(doc.get("id") or _id(doc)),asset,active=True)
                self.notices.append("Prompt Studio ZIPs contain the current project and references. Import the installed library for earlier cuts, renders and motion packets.")
            else:
                raise ValueError("Unknown project ZIP format.")


def import_legacy(project, path, media_root=None, *, cancelled=None):
    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise ValueError("Legacy source does not exist.")
    if path.is_dir() and (path/"data").is_dir() and not (path/"projects").is_dir() and not (path/"studio.sqlite").is_file():
        path = path/"data"
    if path.is_dir() and (path/"studio.sqlite").is_file():
        path = path/"studio.sqlite"
    with project.transaction() as state:
        importer = _Import(project,state,media_root,cancelled)
        importer.check_cancelled()
        if path.is_dir() and any((path/name).is_dir() for name in ("projects","stories","films","video_runs")):
            importer.prompt_library(path)
        elif path.is_file() and path.suffix.lower() in (".sqlite",".db",".sqlite3"):
            importer.atelier_db(path)
        elif path.is_file() and path.suffix.lower()==".zip":
            importer.portable_zip(path)
        elif path.is_file() and path.suffix.lower()==".json":
            doc = _json(path)
            if "shots" not in doc or "assets" not in doc:
                raise ValueError("Choose a Prompt Studio project or its installed data directory.")
            importer.prompt_project(doc,"projects/"+path.name,lambda a:importer.prompt_asset(path.parent.parent,a),active=True)
        else:
            raise ValueError("Choose an Atelier database, Prompt Studio data directory, or portable project ZIP.")
        importer.check_cancelled()
        return dict(_report(state),notices=importer.notices)


def _replace_refs(data, old, new):
    if isinstance(data, list):
        return [_replace_refs(x,old,new) for x in data]
    if isinstance(data, dict):
        return {k:_replace_refs(v,old,new) for k,v in data.items()}
    return new if data == old else data


def relink(project, missing_id, path):
    path = Path(path).expanduser().resolve()
    with project.transaction() as state:
        if missing_id not in state["missing"]:
            if missing_id in state["legacy"].get("resolved",{}):
                return _report(state)
            raise ValueError("Unknown missing-file entry.")
        item = state["missing"][missing_id]
        importer = _Import(project,state,None)
        if item["kind"] in ("story","run"):
            doc = _json(path)
            if doc.get("id") != item["source"][-1]:
                raise ValueError("Selected history has a different ID.")
            if item["kind"] == "story":
                importer.story(doc, "stories/"+doc["id"]+".json")
            else:
                importer.run(path.parent.parent.parent,path.parent,doc)
            importer.resolved[missing_id] = doc["id"]
        else:
            with path.open("rb") as handle:
                record = project.store_stream(handle,path.name,expected_hash=item.get("sha256"))
                stat = os.fstat(handle.fileno())
            state["assets"].setdefault(record["id"],record)
            if item["kind"] == "packet":
                take = state["takes"][_id("prompt-run",item["source"][-1])]
                take["packet"] = packet_info(project._asset_path(state,record["id"]),take["settings"])
                take["settings"]["motion_revision"] = f"{stat.st_mtime_ns:x}-{stat.st_size:x}"
            importer.resolved[missing_id] = record["id"]
            for group in ("revisions","takes","shots"):
                state[group] = _replace_refs(state[group],missing_id,record["id"])
            for entry in importer.catalog.values():
                if "assets" in entry:
                    entry["assets"] = _replace_refs(entry["assets"],missing_id,record["id"])
        state["missing"].pop(missing_id,None)
        return _report(state)
