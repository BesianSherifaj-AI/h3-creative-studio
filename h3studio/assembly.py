"""Review and assemble native H3 packets while retaining editorial trims separately."""
from __future__ import annotations

import copy
import hashlib
import os
import shutil
import uuid
from fractions import Fraction
from pathlib import Path

import httpx

from . import generation, media
from vendor.prompt import comfy_transfer
from vendor.prompt.resources import local_url


def _card(client, base, selector):
    response = client.get(base + "/mmh3_media/file_info", params={"file": selector}, timeout=30)
    if response.status_code == 404:
        return {}
    response.raise_for_status()
    return response.json()


def _packet(project, take, client, base, available):
    packet = take.get("packet") or {}
    saved = take["settings"]
    asset = take.get("packet_asset")
    if not asset or not packet.get("id") or not isinstance(saved.get("graph"), dict):
        raise ValueError("Each selected take needs its archived sampler packet and original H3 workflow.")
    path = project.asset_path(asset)
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != asset:
            raise ValueError("The archived master source changed. Relink the original packet.")
    selector, token = saved.get("motion_selector"), saved.get("motion_revision")
    card = _card(client, base, selector) if selector and selector in available else {}
    if not token or card.get("revision") != token or card.get("id") != packet["id"] or not card.get("has", {}).get("latent"):
        selector, token = generation.restore_packet(base, path, {"sha256":asset,"packet_id":packet["id"]}, client)
    return {"take":take["id"], "packet_id":packet["id"], "sha256":asset, "selector":selector, "revision":token}


def _entry(project_id, take, item, previous):
    settings = take["settings"]
    graph = copy.deepcopy(settings["graph"])
    manifest = {key:copy.deepcopy(value) for key,value in settings.items()
                if key not in ("graph","workflow","devices","guides")}
    if previous:
        parent = manifest.get("parent") or {}
        mmh3 = manifest.get("mmh3") or {}
        if (take.get("parent_take") != previous["take"] or parent.get("take_id") != previous["take"]
                or parent.get("sha256") != previous["sha256"] or mmh3.get("source_packet_id") != previous["packet_id"]):
            raise ValueError("The selected takes must follow the exact saved parent take and packet in order.")
        original = "output::" + str(mmh3.get("source") or "").removeprefix("output::")
        loads = [n for n in graph.values() if n["class_type"] == "MMH3Load"]
        if len(loads) != 1 or loads[0]["inputs"].get("file") != original or loads[0]["inputs"].get("path_override"):
            raise ValueError("The stored workflow loads a different parent packet.")
        # A checked content-identical packet may have a different current filename.
        # Change only the copy used by the master's lineage preflight.
        loads[0]["inputs"]["file"] = previous["selector"]
        mmh3["source"] = previous["selector"].removeprefix("output::")
    manifest["comfy_url"] = item["worker"]
    return {"run":{"id":take["id"],"status":"succeeded","project_id":project_id,"continuation_source":item["selector"]},
            "transfer":{"id":manifest.get("transfer_id"),"comfy_url":item["worker"],"prompt":graph,"manifest":manifest}}


def prepare_master(project, take_ids, worker, *, client=None):
    if client is None:
        with httpx.Client(trust_env=False) as owned:
            return prepare_master(project, take_ids, worker, client=owned)
    base = local_url(worker)
    doc = project.state()
    selected = [doc["shots"][s].get("selected_take") for s in doc["cut"]] if take_ids is None else take_ids
    if not isinstance(selected, list) or len(selected) < 2 or len(set(selected)) != len(selected) or any(t not in doc["takes"] for t in selected):
        raise ValueError("Select at least two distinct saved H3 takes in continuation order.")
    _, schema = comfy_transfer._connect({"comfy_urls":[base]}, client)
    from vendor.prompt.mmh3_transfer import mmh3_options
    available = {entry["selector"] for entry in mmh3_options(schema)["mmh3_sources"]}
    entries, sources = [], []
    editorial_duration = Fraction(0)
    for ident in selected:
        take = doc["takes"][ident]
        item = {**_packet(project,take,client,base,available), "worker":base}
        entries.append(_entry(doc["id"],take,item,sources[-1] if sources else None))
        trim = take["settings"].get("delivery_trim") or [0,take["settings"]["frames"]/24]
        start, end = (Fraction(str(x)) for x in trim)
        if not 0 <= start < end or end*24 > take["settings"]["frames"] + Fraction(1,1000000):
            raise ValueError("One take has an invalid editorial trim.")
        editorial_duration += end-start
        sources.append({**item, "delivery_trim_seconds":trim})
    if any(s["selector"] not in available for s in sources):
        _, schema = comfy_transfer._connect({"comfy_urls":[base]},client)
    bundle = media.prepare_h3_join(entries, schema)
    m = bundle["manifest"]
    m["segments"] = [{**source,"master_start_frame":fact["output_frame_start"],"master_end_frame":fact["output_frame_end"],
                      "render_trim":[round(24*x) for x in source["delivery_trim_seconds"]],"overlap_frames":fact["overlap_frames"]}
                     for source,fact in zip(sources,m["source_runs"],strict=True)]
    m["editorial_frames"] = round(editorial_duration * 24)
    bundle["workflow"].setdefault("extra", {})["h3_packet_master"] = copy.deepcopy(m)
    key = bundle["id"]
    with project.transaction() as current:
        current.setdefault("master_plans",{})[key] = bundle
    return {"plan":key,"manifest":m}


def validate_master(bundle, client):
    base = local_url(bundle["comfy_url"])
    _, schema = comfy_transfer._connect({"comfy_urls":[base]}, client)
    m = bundle["manifest"]
    nodes = bundle["prompt"]
    loaders = [nodes[key]["inputs"].get("file") for key in sorted(nodes,key=int) if nodes[key]["class_type"] == "MMH3Load"]
    if loaders != [s["selector"] for s in m["segments"]]:
        raise ValueError("The master graph no longer matches its reviewed packets.")
    comfy_transfer._validate_graph(bundle["prompt"],schema,set())
    for s in m["segments"]:
        card = _card(client,base,s["selector"])
        if card.get("id") != s["packet_id"] or card.get("revision") != s["revision"] or not card.get("has",{}).get("latent"):
            raise ValueError("A reviewed packet changed on the renderer. Prepare the master again.")


def adopt_master(project, job):
    payload = job["payload"]
    doc = project.state()
    if payload.get("studio_project") != doc["id"] or job["status"] != "succeeded":
        raise ValueError("Open the project that owns this completed packet master.")
    previous = doc.get("masters",{}).get(job["id"])
    if previous:
        return {"master":job["id"],"asset":previous["asset"]}
    m = payload["manifest"]
    outputs = [o for o in job["receipt"].get("outputs",[]) if o.get("node_id") == m["video_save_node_id"]]
    if len(outputs) != 1 or not outputs[0].get("sha256"):
        raise ValueError("The master needs one verified video output from its reviewed SaveVideo node.")
    o = outputs[0]
    with Path(o["path"]).open("rb") as stream:
        record = project.store_stream(stream,o["filename"],expected_hash=o["sha256"])
    entry = {"id":job["id"],"asset":record["id"],"manifest":m,"plan":payload["packet_master"]}
    with project.transaction() as current:
        current["assets"].setdefault(record["id"], record)
        current.setdefault("masters",{}).setdefault(job["id"],entry)
    return {"master":job["id"],"asset":record["id"]}


def master_action(studio, project, op, args):
    if op == "prepare_master":
        return prepare_master(project, args.get("takes"), studio.prefs["video_url"])
    doc = project.state()
    if op == "submit_master":
        key = args["plan"]
        bundle = doc["master_plans"][key]
        with httpx.Client(trust_env=False) as client:
            validate_master(bundle, client)
        return studio.queue.enqueue("video",{**bundle,"studio_project":doc["id"],"packet_master":key},job_id=key)
    if op == "export_master":
        asset = doc["masters"][args["master"]]["asset"]
        source = project.asset_path(asset)
        target = Path(args["path"]).resolve()
        if target.suffix.lower() != ".mp4" or target.is_relative_to(project.folder):
            raise ValueError("Choose an MP4 file outside the project.")
        temp = target.with_name("." + target.name + "." + uuid.uuid4().hex + ".part")
        try:
            shutil.copyfile(source,temp)
            os.replace(temp,target)
        finally:
            temp.unlink(missing_ok=True)
        return {"path":str(target)}
    raise ValueError("Unknown packet master operation.")
