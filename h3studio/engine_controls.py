"""Desktop commands for local renderer profiles, status, and explicit starts."""
from __future__ import annotations

from . import runtime
from .application import atomic_json


KINDS = ("video", "image")
CONNECTIONS = ("video_url", "image_url", "director_url", "director_model", "finishing_install")


def _profile(studio, kind):
    return studio.prefs.get("engine_profiles", {}).get(kind, {})


def _validate(kind, values):
    if not isinstance(values, dict):
        raise ValueError("Supply a renderer profile.")
    config = {}
    for name in ("source", "python", "custom_nodes"):
        value = values.get(name)
        if isinstance(value, str) and value.strip():
            config[name] = value.strip()
    if values.get("model_configs"):
        files = values["model_configs"]
        if not isinstance(files, list) or any(not isinstance(p, str) or not p.strip() for p in files):
            raise ValueError("Give one model YAML path per line.")
        config["model_configs"] = [p.strip() for p in files]
    if values.get("port") not in (None, ""):
        config["port"] = values["port"]
    runtime.installation(kind, config)  # Verifies the port. Missing local installs can still be configured.
    return config


def _save(studio, prefs):
    updated = {**studio.prefs, **prefs}
    atomic_json(studio.prefs_path, updated)
    studio.prefs.update(prefs)


def _describe(studio, kind):
    cfg = _profile(studio, kind)
    return dict(profile=cfg, detected=runtime.installation(kind, cfg),
                status=runtime.engine_status(studio.home, kind, cfg))


def engine_action(studio, op, args):
    kind = args.get("kind")
    if kind is not None and kind not in KINDS:
        raise ValueError("Choose the image or video renderer.")
    if op == "engine_status":
        if kind:
            return _describe(studio, kind)
        return dict(engines={k:_describe(studio,k) for k in KINDS},
                    connections={k:studio.prefs.get(k,"") for k in CONNECTIONS})
    if op == "engine_config":
        profiles = dict(studio.prefs.get("engine_profiles", {}))
        changes = {kind:args.get("profile", {})} if kind else args.get("profiles", {})
        if not isinstance(changes,dict) or any(k not in KINDS for k in changes):
            raise ValueError("Unknown renderer profile.")
        for engine, values in changes.items():
            profiles[engine] = _validate(engine,values)
        # Validate every profile ahead of the single write, including the pair of listening ports.
        ports = [runtime.installation(k,profiles.get(k,{}))["port"] for k in KINDS]
        if len(set(ports)) != len(ports):
            raise ValueError("Choose a different port for each renderer.")
        connections = args.get("connections", {})
        _save(studio, {"engine_profiles":profiles,
                       **{k:str(connections[k]).strip() for k in CONNECTIONS if k in connections}})
        return {"saved":True}
    if kind is None or op not in ("start_engine", "stop_engine"):
        raise ValueError("Unknown engine control.")
    cfg = _profile(studio, kind)
    if op == "stop_engine":
        return runtime.stop_engine(studio.home, kind, cfg)
    result = runtime.start_engine(studio.home, kind, cfg)
    if result["owned"] and result["state"] in ("starting", "ready"):
        _save(studio, {kind+"_url":result["url"]})
    return result
