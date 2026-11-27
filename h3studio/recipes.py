"""Editable project presets and shot recipes, independent of renderer uploads and take IDs."""
import copy

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


def effective(doc, shot):
    default = doc.get("recipe_default", {})
    selected = doc["takes"].get(shot.get("selected_take"), {}).get("settings", {})
    archived = shot.get("settings") or selected
    saved_plans = [plan for plan in doc.get("prepared", {}).values() if plan.get("shot") == shot["id"]]
    last = saved_plans[-1] if saved_plans else {}
    source = shot.get("recipe") or last.get("request") or archived.get("request")
    if not source:
        source = {k:archived[k] for k in ("seed","mode","prompt") if k in archived}
        prompt = source.get("prompt")
        if isinstance(prompt,dict):
            source["prompt"] = next((prompt[k] for k in ("text","prompt","compiled_prompt") if isinstance(prompt.get(k),str)), "")
        source["settings"] = {k:archived[k] for k in ("resolution","quality","aspect_ratio","loras_by_mode","steps","save_mmh3") if k in archived}
        if archived.get("loras") and archived.get("mode") and "loras_by_mode" not in source["settings"]:
            source["settings"]["loras_by_mode"] = {archived["mode"]:archived["loras"]}
    result = {**default, **recipe(source), "settings":{**default.get("settings", {}), **source.get("settings", {})}}
    if shot.get("director_prompt") and not shot.get("recipe"):
        result["prompt"] = shot["director_prompt"]
    return result


def action(project, op, fields):
    doc = project.state()
    shot = doc["shots"][fields["shot"]]
    if op == "recipe_get":
        return {"recipe":effective(doc, shot), "default":doc.get("recipe_default"),
                "presets":doc.get("recipe_presets", {})}
    value = recipe(fields["recipe"])
    with project.transaction() as state:
        if op == "recipe_save":
            state["shots"][shot["id"]]["recipe"] = value
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
