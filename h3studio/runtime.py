"""Start and identify isolated, loopback ComfyUI engines owned by this desktop installation."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import time
import uuid

import httpx
import psutil

from .jobs import game_guard


_PORTS = {"video":8021, "image":8022}
_VIDEO_NODES = ["ComfyUI_mmh3_media", "ComfyUI-PlagueKind-Nodes"]


def _base(home,kind):
    if kind not in _PORTS:
        raise ValueError("Choose the image or video engine.")
    return Path(home).resolve()/"engines"/kind


def _port(kind,config):
    port=(config or {}).get("port",_PORTS[kind])
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Use a loopback engine port from 1024 to 65535.")
    return port


def _desktop_models(appdata,source):
    desktop=appdata/"Comfy Desktop"
    try:
        entries=json.loads((desktop/"installations.json").read_text(encoding="utf-8-sig"))
        for entry in entries:
            ident=entry.get("id","")
            if Path(ident).name!=ident or not _same_path(entry.get("installPath",""),source):
                continue
            path=desktop/"instance-model-paths"/(ident+".yaml")
            if path.is_file():
                return path
    except (OSError,ValueError,AttributeError,TypeError):
        pass
    return None


def installation(kind,config=None):
    """Discover installed code and model-path configs without touching either installation."""
    if kind not in _PORTS:
        raise ValueError("Choose the image or video engine.")
    config=config or {}
    appdata=Path(os.environ.get("APPDATA",Path.home()/".config"))
    instance=Path.home()/"ComfyUI-Installs"/("H3-Benchmark" if kind=="video" else "H3-Image")
    source=Path(config.get("source") or (instance/"ComfyUI")).expanduser().resolve()
    instance=source.parent
    python=Path(config.get("python") or (instance/".venv"/("Scripts/python.exe" if os.name=="nt" else "bin/python"))).expanduser().resolve()
    custom_nodes=Path(config.get("custom_nodes") or instance/"base"/"custom_nodes").expanduser().resolve()
    if config.get("model_configs") is not None:
        values=config["model_configs"]
        if not isinstance(values,list):
            raise ValueError("Supply model configuration files as a list of YAML paths.")
        models=[Path(v).expanduser().resolve() for v in values]
    elif kind=="image":
        atlas=(Path(__file__).resolve().parents[2]/"AI-IMAGE-STUDIO"/"model-paths.yaml")
        models=[p for p in (instance/"base"/"model-paths.yaml",atlas) if p.is_file()]
    else:
        first=instance/"base"/"h3-model-paths.yaml"
        models=[first] if first.is_file() else []
        extra=_desktop_models(appdata,source)
        if extra:
            models.append(extra)
    missing=[str(p) for p in (python,source/"main.py",*models) if not p.is_file()]
    if kind=="video":
        missing.extend(str(custom_nodes/n) for n in _VIDEO_NODES if not (custom_nodes/n).is_dir())
    local_models=[path for path in (instance/"base"/"models",source/"models") if path.is_dir()]
    if not models and not local_models:
        missing.append("ComfyUI model paths")
    return dict(source=str(source),python=str(python),custom_nodes=str(custom_nodes),model_configs=[str(p) for p in models],
                native_models=[str(p) for p in local_models],port=_port(kind,config),missing=missing)


def _write(path,value):
    unique=path.with_name("."+uuid.uuid4().hex+".tmp")
    try:
        unique.write_text(json.dumps(value,indent=2),encoding="utf-8")
        os.replace(unique,path)
    finally:
        unique.unlink(missing_ok=True)


def _read(base):
    try:
        return json.loads((base/"owner.json").read_text(encoding="utf-8"))
    except (OSError,ValueError):
        return {}


@contextmanager
def _lock(base):
    base.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(base/"control.sqlite3",timeout=2)
    try:
        db.execute("BEGIN IMMEDIATE")
        yield
        db.commit()
    finally:
        db.close()


def _same_path(a,b):
    return os.path.normcase(os.path.abspath(a))==os.path.normcase(os.path.abspath(b))


def _matching(process,receipt,base):
    try:
        args=process.cmdline()
        return (any(_same_path(a,receipt["main"]) for a in args if a.lower().endswith("main.py"))
                and _same_path(args[args.index("--base-directory")+1],str(base))
                and args[args.index("--port")+1]==str(receipt["port"])
                and args[args.index("--listen")+1]=="127.0.0.1")
    except (ValueError,KeyError,IndexError,psutil.Error,OSError):
        return False


def _owned(base,receipt):
    if receipt.get("closed") or not receipt.get("pid"):
        return []
    try:
        parent=psutil.Process(receipt["pid"])
        if abs(parent.create_time()-receipt["created"])>.001 or not _matching(parent,receipt,base):
            return []
        children=[p for p in parent.children(recursive=True) if _matching(p,receipt,base)]
        return [parent,*children]
    except (psutil.Error,OSError,KeyError):
        return []


def _listening(port):
    try:
        with socket.create_connection(("127.0.0.1",port),timeout=.15):
            return True
    except OSError:
        return False


def _listener_owned(processes,port):
    for process in processes:
        try:
            if any(c.status==psutil.CONN_LISTEN and c.laddr.port==port for c in process.net_connections("inet")):
                return True
        except (psutil.Error,OSError):
            continue
    return False


def _request(port,route,data=None):
    with httpx.Client(base_url=f"http://127.0.0.1:{port}",timeout=2,trust_env=False) as client:
        resp=client.get(route) if data is None else client.post(route,json=data)
        resp.raise_for_status()
        return resp.json() if resp.content else {}


def _status(base,port,guard):
    receipt=_read(base)
    procs=_owned(base,receipt)
    if procs:
        port=receipt["port"]
    result=dict(state="stopped", owned=bool(procs), url=f"http://127.0.0.1:{port}",port=port,devices=[],device="",
                guard=guard,pid=receipt.get("pid") if procs else None,log=str(base/"studio-worker.log"),
                detail="Engine is stopped.")
    listening=_listening(port)
    if listening and not _listener_owned(procs,port):
        result.update(state="occupied",detail="Port is in use by an externally managed process.")
    elif listening:
        try:
            stats=_request(port,"/system_stats")
            devices=[d.get("name") or d.get("type") for d in stats.get("devices",[])]
            result.update(state="ready",devices=devices,device=", ".join(v for v in devices if v),
                          detail="Engine is ready.")
        except (httpx.HTTPError,ValueError):
            result.update(state="starting",detail="Waiting for the engine API.")
    elif procs:
        result.update(state="starting",detail="Worker is loading. Check its log for progress.")
    elif receipt and not receipt.get("closed"):
        result.update(state="failed",detail="Previous engine process exited. Check the worker log.")
    elif guard:
        result.update(state="blocked",detail="Game guard is active.")
    return result


def engine_status(home,kind,config=None):
    return _status(_base(home,kind),_port(kind,config),game_guard())


def _command(base,kind,settings):
    if settings["missing"]:
        raise ValueError("Missing installed engine files: "+", ".join(settings["missing"]))
    for name in ("input","output","temp","user","custom_nodes"):
        (base/name).mkdir(parents=True,exist_ok=True)
    args=[settings["python"],"-B","-s",str(Path(settings["source"])/"main.py"),"--disable-auto-launch",
          "--listen","127.0.0.1","--port",str(settings["port"]),"--base-directory",str(base)]
    for name in ("user","input","output","temp"):
        args.extend(["--"+name+"-directory",str(base/name)])
    models=list(settings["model_configs"])
    if not models:
        roots={}
        for index,path in enumerate(map(Path,settings["native_models"])):
            roots[f"studio_models_{index}"]={"base_path":str(path),
                **{p.name:p.name+"/" for p in path.iterdir() if p.is_dir() and not p.name.startswith(".")}}
        model_file=base/"studio-models.yaml"
        _write(model_file,roots)
        models.append(str(model_file))
    if kind=="video":
        nodes=Path(settings["custom_nodes"])
        nodefile=base/"studio-custom-nodes.yaml"
        # JSON strings are valid YAML scalars and keep Windows paths intact.
        nodefile.write_text("studio_nodes:\n  base_path: "+json.dumps(nodes.parent.as_posix())+
                            "\n  custom_nodes: "+json.dumps(nodes.name+"/")+"\n",encoding="utf-8")
        models.append(str(nodefile))
    if models:
        args.extend(["--extra-model-paths-config",*models])
    args.extend(["--disable-all-custom-nodes","--preview-method","none"])
    if kind=="video":
        args.extend(["--whitelist-custom-nodes",*_VIDEO_NODES,"--disable-api-nodes"])
    else:
        args.extend(["--offline","--cache-none","--reserve-vram","3","--disable-dynamic-vram"])
    return args


def _spawn(args,base):
    env=os.environ.copy()
    env.update(PYTHONDONTWRITEBYTECODE="1",HF_HUB_OFFLINE="1",HF_DATASETS_OFFLINE="1",TRANSFORMERS_OFFLINE="1")
    env.update(TMP=str(base/"temp"),TEMP=str(base/"temp"),TORCH_EXTENSIONS_DIR=str(base/"cache"/"extensions"),
               TORCHINDUCTOR_CACHE_DIR=str(base/"cache"/"inductor"),TRITON_CACHE_DIR=str(base/"cache"/"triton"))
    with (base/"studio-worker.log").open("ab") as log:
        log.write(f"\nStarting Studio engine at {time.strftime('%Y-%m-%d %H:%M:%S')}\n".encode())
        log.flush()
        return subprocess.Popen(args,cwd=base,stdin=subprocess.DEVNULL,stdout=log,stderr=log,env=env,
                                creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))


def start_engine(home,kind,config=None):
    base=_base(home,kind)
    settings=installation(kind,config)
    with _lock(base):
        status=_status(base,settings["port"],game_guard())
        if status["guard"]:
            return {**status,"state":"blocked","detail":"Game guard is active."}
        if status["state"] in ("ready","occupied","starting"):
            return status
        args=_command(base,kind,settings)
        proc=_spawn(args,base)
        try:
            announced=psutil.Process(proc.pid)
            receipt=dict(pid=proc.pid,created=announced.create_time(),port=settings["port"],
                         main=str(Path(settings["source"])/"main.py"))
            _write(base/"owner.json",receipt)
        except (psutil.Error,OSError):
            # We just created the process and still hold the OS handle.
            if proc.poll() is None:
                proc.terminate()
            raise ValueError("Engine did not start cleanly. Check the worker log.") from None
        return {**status,"state":"starting","owned":True,"pid":proc.pid,"detail":"Engine is starting."}


def _idle(port):
    queue=_request(port,"/queue")
    if not isinstance(queue.get("queue_running"),list) or not isinstance(queue.get("queue_pending"),list):
        raise ValueError("Could not verify the engine's queue.")
    return not queue["queue_running"] and not queue["queue_pending"]


def stop_engine(home,kind,config=None):
    base=_base(home,kind)
    port=_port(kind,config)
    if not (base/"owner.json").is_file():
        return engine_status(home,kind,config)
    with _lock(base):
        receipt=_read(base)
        procs=_owned(base,receipt)
        guard=game_guard()
        status=_status(base,port,guard)
        if not procs or status["state"]=="occupied":
            return status
        if _listening(status["port"]):
            try:
                if not _listener_owned(procs,status["port"]) or not _idle(status["port"]):
                    return {**status,"detail":"Waiting for the active or pending renderer jobs."}
            except (httpx.HTTPError,ValueError):
                return {**status,"detail":"The renderer queue could not be verified. Try again once it is ready."}
        for p in reversed(procs):
            try:
                if _matching(p,receipt,base):
                    p.terminate()
            except psutil.NoSuchProcess:
                pass
        _,alive=psutil.wait_procs(procs,timeout=3)
        if alive:
            return {**status,"state":"stopping","detail":"Waiting for owned processes to exit."}
        _write(base/"owner.json",{**receipt,"closed":True})
        return _status(base,port,guard)


def guard_engines(home):
    # Only look at managed engines. A fresh desktop has nothing to release.
    marked=[k for k in _PORTS if (_base(home,k)/"owner.json").is_file() and not _read(_base(home,k)).get("closed")]
    if not marked or not game_guard():
        return []
    return [stop_engine(home,k) for k in marked]


def release_idle_peers(home,active_url):
    """Ask an idle owned peer to release cached models before another worker takes the GPU."""
    released=[]
    for kind in _PORTS:
        base=_base(home,kind)
        receipt=_read(base)
        if not receipt or receipt.get("closed"):
            continue
        port=receipt["port"]
        if active_url.rstrip("/") in (f"http://127.0.0.1:{port}",f"http://localhost:{port}"):
            continue
        procs=_owned(base,receipt)
        if procs and _listener_owned(procs,port):
            try:
                if _idle(port):
                    _request(port,"/free",{"unload_models":True,"free_memory":True})
                    released.append(kind)
            except (httpx.HTTPError,ValueError):
                continue
    return released
