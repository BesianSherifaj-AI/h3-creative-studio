"""Editable project presets and shot recipes, independent of renderer uploads and take IDs."""
import copy
import hashlib

MODES = ("t2va", "i2va", "l2va", "fl2va", "ref2va")


def recipe(value):
    settings = value.get("settings", {})
    if not isinstance(settings, dict):
        raise ValueError("Render settings must be a JSON object.")
    result = {k:copy.deepcopy(value[k]) for k in ("prompt", "mode", "seed") if k in value}
    if "mode" in result and result["mode"] not in MODES:
        raise ValueError("Choose a supported H3 mode.")
    if "seed" in result and (type(result["seed"]) is not int or not 0 <= result["seed"] <= 2**53 - 1):
        raise ValueError("Choose a valid whole-number seed.")
    if "prompt" in result and not isinstance(result["prompt"], str):
        raise ValueError("Enter a text prompt.")
    loras = settings.get("loras_by_mode", {})
    if not isinstance(loras, dict) or any(k not in MODES or not isinstance(v, list) for k,v in loras.items()):
        raise ValueError("Ordered LoRAs must be lists keyed by H3 mode.")
    result["settings"] = copy.deepcopy(settings)
    return result


def take_settings(archived, plan=None):
    """Recover editable inputs from a request or a previously archived H3 graph."""
    plan = plan or {}
    request = archived.get("request") or plan.get("request") or {}
    source = {k:archived[k] for k in ("mode","seed","prompt") if k in archived}
    source.update({k:request[k] for k in ("mode","seed","prompt") if k in request})
    prompt = source.get("prompt")
    if isinstance(prompt,dict):
        prompt = next((prompt[k] for k in ("compiled_prompt","text","prompt") if isinstance(prompt.get(k),str)), None)
    if not prompt:
        candidates = [plan.get("prompt_text"),archived.get("compiled_prompt")]
        for node in (archived.get("graph") or {}).values():
            fields = node.get("inputs",{})
            candidates.extend(fields.get(k) for k in ("prompt_override","prompt","text"))
        digest = archived.get("prompt_sha256")
        prompt = next((v for v in candidates if isinstance(v,str) and v and
                       (not digest or hashlib.sha256(v.encode()).hexdigest()==digest)),None)
    if prompt:
        source["prompt"] = prompt
    else:
        source.pop("prompt",None)
    settings = {k:archived[k] for k in ("resolution","quality","aspect_ratio","loras_by_mode","steps","save_mmh3",
                                         "experimental_preview","align_motion_ending") if k in archived}
    if "save_enabled" in (archived.get("mmh3") or {}):
        settings["save_mmh3"] = archived["mmh3"]["save_enabled"]
    if (archived.get("timeline") or {}).get("align_motion_ending"):
        settings["align_motion_ending"] = True
    settings.update(request.get("settings") or {})
    # Continuation manifests report effective parent dimensions as "source".
    # The transfer builder derives those dimensions again from the verified packet.
    for key in ("resolution","aspect_ratio"):
        if settings.get(key)=="source":
            del settings[key]
    mode = source.get("mode")
    if mode and archived.get("loras"):
        stacks = copy.deepcopy(settings.get("loras_by_mode") or {})
        stacks.setdefault(mode,[{k:x[k] for k in ("name","strength","enabled") if k in x} for x in archived["loras"]])
        settings["loras_by_mode"] = stacks
    return recipe({**source,"settings":settings})


def take_source(doc, shot, ident):
    take = doc["takes"].get(ident)
    if not take or (ident not in shot["takes"] and not (shot.get("branch_action")=="retry" and ident==shot.get("parent_take"))):
        raise ValueError("Choose a take belonging to this shot.")
    archived = take["settings"]
    plan = next((v for k,v in doc.get("prepared",{}).items()
                 if k.replace("-","")==str(archived.get("job_id","")).replace("-","") or
                 (archived.get("transfer_id") and v.get("bundle",{}).get("id")==archived["transfer_id"])),{})
    inputs = take_settings(archived,plan)
    if not inputs.get("prompt") or not inputs.get("mode") or "seed" not in inputs:
        raise ValueError("This take has no complete saved H3 generation recipe.")
    guides = copy.deepcopy(take.get("guides") or plan.get("guides") or [])
    if not guides and (archived.get("guides") or archived.get("references")):
        guides = manifest_guides(doc,archived)
    parent = archived.get("parent")
    if (archived.get("mmh3") or {}).get("continuation") and not parent:
        parent_take = doc["takes"].get(take.get("parent_take"),{})
        if parent_take.get("packet",{} ) and parent_take["packet"]["id"]==archived["mmh3"].get("source_packet_id"):
            parent = {"take_id":parent_take["id"],"use_original_motion":True,"packet_id":parent_take["packet"]["id"]}
        else:
            raise ValueError("Relink this take's original parent motion packet before replay.")
    if parent and not doc["takes"].get(parent.get("take_id"),{}).get("packet_asset"):
        raise ValueError("Relink this take's original parent motion packet before replay.")
    start = archived.get("start_choice")
    request = archived.get("request") or plan.get("request") or {}
    if not start and request.get("start_mode"):
        start = {"mode":request["start_mode"],"revision":request.get("start_revision")}
    return {"id":ident,"recipe":inputs,"guides":guides,"parent":copy.deepcopy(parent),
            "duration":archived.get("duration",take["duration"]),"start":start,"source_parent_take":take.get("parent_take")}


def manifest_guides(doc, archived):
    """Recover older timed/reference records only when their original bytes or revision exist."""
    result = []
    references = archived.get("references") or []
    reference_guides = {}
    records = [(ref,"reference") for ref in references] + [(g,"timed") for g in archived.get("guides",[])]
    for ref,usage in records:
        role = ref.get("role") or ref.get("kind","image")
        frame = ref.get("frame",round(archived.get("duration",5)*24)-1 if role=="last_frame" else 0)
        guide = {"frame":frame,"role":role,"usage":usage}
        key = ref.get("asset_id")
        rev = ref.get("revision")
        if rev:
            if rev not in doc["revisions"]:
                raise ValueError("Relink this take's original composition revision before replay.")
            guide["revision"] = rev
        else:
            asset = ref.get("source_sha256") or reference_guides.get(key,{}).get("asset") or ref.get("sha256")
            if not asset or asset not in doc["assets"]:
                raise ValueError("Relink this take's original guide media before replay.")
            guide["asset"] = asset
        guide.update({k:ref[k] for k in ("semantic_role","clip_start_seconds","clip_end_seconds","prompt_tag") if k in ref})
        if ref.get("audio_enabled") or ref.get("with_audio"): guide["with_audio"] = True
        if ref.get("tag"): guide["prompt_tag"] = ref["tag"]
        if key and usage=="reference":
            reference_guides[key] = guide
        if usage=="timed" and key in reference_guides:
            paired = reference_guides[key]
            if paired.get("revision")!=guide.get("revision") or paired.get("asset")!=guide.get("asset"):
                raise ValueError("The saved reference and timed guide disagree on their source.")
            # A relocated endpoint and a dual-use reference each came from one input.
            paired.update(frame=frame,usage="both")
            continue
        result.append(guide)
    return result


def effective(doc, shot):
    default = doc.get("recipe_default", {})
    first = doc["takes"].get(next(iter(shot["takes"]),None), {}).get("settings", {})
    archived = shot.get("settings") or first
    saved_plans = [plan for plan in doc.get("prepared", {}).values() if plan.get("shot") == shot["id"] and not plan.get("source_take")]
    last = saved_plans[-1] if saved_plans else {}
    workspace = doc.get("recipe_drafts",{}).get(shot["id"],{})
    source = workspace.get("recipe") or shot.get("recipe") or last.get("request") or take_settings(archived)
    result = {**default, **recipe(source), "settings":{**default.get("settings", {}), **source.get("settings", {})}}
    if shot.get("director_prompt") and not (workspace or shot.get("recipe")):
        result["prompt"] = shot["director_prompt"]
    return result


def action(project, op, fields):
    doc = project.state()
    shot = doc["shots"][fields["shot"]]
    if op == "recipe_get":
        ident = shot.get("selected_take") or (shot.get("parent_take") if shot.get("branch_action")=="retry" else None)
        selected,reason = None,""
        if ident:
            try:
                selected = take_source(doc,shot,ident)
            except ValueError as e:
                reason = str(e)
        return {"recipe":effective(doc, shot), "default":doc.get("recipe_default"),
                "draft_start":doc.get("recipe_drafts",{}).get(shot["id"],{}).get("start"),"selected":selected,
                "unavailable":reason,"presets":doc.get("recipe_presets", {})}
    value = recipe(fields["recipe"])
    with project.transaction() as state:
        if op == "recipe_save":
            state["shots"][shot["id"]]["recipe"] = value
            state.get("recipe_drafts",{}).pop(shot["id"],None)
        elif op == "recipe_draft":
            state.setdefault("recipe_drafts",{})[shot["id"]] = {"recipe":value,"start":fields.get("start")}
        elif op == "recipe_default":
            state["recipe_default"] = value
        elif op == "recipe_preset":
            name = fields.get("name", "").strip()
            if not 0 < len(name) <= 100:
                raise ValueError("Enter a preset name of up to 100 characters.")
            state.setdefault("recipe_presets", {})[name] = value
        else:
            raise ValueError("Unknown shot recipe command.")
    return value
