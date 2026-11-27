"""Engine startup controls exercised without launching ComfyUI or accessing a GPU."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from h3studio import runtime


def fake_install(tmp_path, kind):
    root=tmp_path/f"{kind}-upstream"
    src=root/"ComfyUI"
    src.mkdir(parents=True)
    (src/"main.py").write_text("# fixture")
    exe=root/"python.exe"
    exe.write_bytes(b"fixture")
    yml=root/"model-paths.yaml"
    yml.write_text("fixture: {}")
    nodes=root/"existing_nodes"
    for n in runtime._VIDEO_NODES:
        (nodes/n).mkdir(parents=True)
    cfg=dict(source=str(src),python=str(exe),model_configs=[str(yml)],custom_nodes=str(nodes))
    return root,cfg


def test_command_writes_only_isolated_directories(tmp_path):
    home=tmp_path/"Studio home"
    for kind,port in (("video",8021),("image",8022)):
        root,cfg=fake_install(tmp_path,kind)
        start={p.relative_to(root):p.stat().st_mtime_ns for p in root.rglob("*")}
        settings=runtime.installation(kind,cfg)
        assert not settings["missing"]
        base=runtime._base(home,kind)
        args=runtime._command(base,kind,settings)
        assert args[args.index("--listen")+1]=="127.0.0.1"
        assert args[args.index("--port")+1]==str(port)
        for name in ("temp","input","output","user"):
            assert Path(args[args.index("--"+name+"-directory")+1])==base/name
            assert (base/name).is_dir()
        assert (base/"custom_nodes").is_dir()
        if kind=="video":
            assert "ComfyUI_mmh3_media" in args
            assert "existing_nodes/" in (base/"studio-custom-nodes.yaml").read_text()
        assert start=={p.relative_to(root):p.stat().st_mtime_ns for p in root.rglob("*")}


def test_foreign_listener_and_game_guard_do_not_launch_or_kill(tmp_path,monkeypatch):
    _,cfg=fake_install(tmp_path,"video")
    spawn=Mock(side_effect=AssertionError("must not start"))
    monkeypatch.setattr(runtime,"_spawn",spawn)
    monkeypatch.setattr(runtime,"game_guard",lambda:[])
    monkeypatch.setattr(runtime,"_listening",lambda port:True)
    s=runtime.start_engine(tmp_path,"video",cfg)
    assert s["state"]=="occupied" and not s["owned"]
    assert runtime.stop_engine(tmp_path,"video",cfg)["state"]=="occupied"
    monkeypatch.setattr(runtime,"_listening",lambda port:False)
    monkeypatch.setattr(runtime,"game_guard",lambda:["TestGame.exe"])
    assert runtime.start_engine(tmp_path,"video",cfg)["state"]=="blocked"
    assert not spawn.called


def test_repeated_start_pid_identity_busy_queue_and_release(tmp_path,monkeypatch):
    _,cfg=fake_install(tmp_path,"video")
    main=str(Path(cfg["source"])/"main.py")
    base=runtime._base(tmp_path,"video")
    class FakeProcess:
        pid=921
        when=52.125
        listening=False
        live=True
        def cmdline(self):
            return [cfg["python"],"-s",main,"--port","8021","--base-directory",str(base),"--listen","127.0.0.1"]
        def create_time(self): return self.when
        def children(self,**kwargs): return []
        def net_connections(self,kind):
            return [SimpleNamespace(status=runtime.psutil.CONN_LISTEN,laddr=SimpleNamespace(port=8021))] if self.listening else []
        def terminate(self): self.live=False
    fake=FakeProcess()
    def proc(pid):
        if not fake.live:
            raise runtime.psutil.NoSuchProcess(pid)
        return fake
    monkeypatch.setattr(runtime,"_listening",lambda port:fake.listening and fake.live)
    monkeypatch.setattr(runtime,"game_guard",lambda:[])
    monkeypatch.setattr(runtime.psutil,"Process",proc)
    monkeypatch.setattr(runtime.psutil,"wait_procs",lambda p,timeout:(p,[]))
    launch=Mock(return_value=fake)
    monkeypatch.setattr(runtime,"_spawn",launch)
    a=runtime.start_engine(tmp_path,"video",cfg)
    assert a["state"]=="starting" and a["owned"]
    assert runtime.start_engine(tmp_path,"video",cfg)["pid"]==fake.pid and launch.call_count==1
    fake.listening=True
    busy=True
    calls=[]
    def request(port,route,data=None):
        calls.append((route,data))
        if route=="/system_stats":
            return {"devices":[{"name":"fixture CUDA"}]}
        if route=="/queue":
            return {"queue_running":[["other"]] if busy else [],"queue_pending":[]}
        return {}
    monkeypatch.setattr(runtime,"_request",request)
    assert runtime.engine_status(tmp_path,"video")["device"]=="fixture CUDA"
    assert "Waiting" in runtime.stop_engine(tmp_path,"video")["detail"]
    assert fake.live
    assert not runtime.release_idle_peers(tmp_path,"http://127.0.0.1:8022")
    busy=False
    assert runtime.release_idle_peers(tmp_path,"http://127.0.0.1:8022")==["video"]
    assert [c for c in calls if c[0]=="/free"]==[("/free",{"unload_models":True,"free_memory":True})]
    # If the PID has been reused, even an identical port & command is insufficient ownership.
    fake.when+=4
    assert runtime.engine_status(tmp_path,"video")["state"]=="occupied"
    assert not runtime.stop_engine(tmp_path,"video")["owned"] and fake.live
    fake.when-=4
    monkeypatch.setattr(runtime,"game_guard",lambda:["Fixture"])
    result=runtime.guard_engines(tmp_path)
    assert not fake.live and result[0]["state"]=="blocked"
    assert json.loads((base/"owner.json").read_text())["closed"]


def test_missing_engine_never_launches(tmp_path,monkeypatch):
    _,cfg=fake_install(tmp_path,"image")
    Path(cfg["python"]).unlink()
    monkeypatch.setattr(runtime,"_listening",lambda port:False)
    monkeypatch.setattr(runtime,"game_guard",lambda:[])
    with pytest.raises(ValueError,match="Missing installed engine files"):
        runtime.start_engine(tmp_path,"image",cfg)


def test_model_discovery_matches_source_record_and_falls_back_to_native(tmp_path,monkeypatch):
    root,cfg=fake_install(tmp_path,"video")
    del cfg["model_configs"]
    desktop=tmp_path/"roaming"/"Comfy Desktop"
    configs=desktop/"instance-model-paths"
    configs.mkdir(parents=True)
    for ident in ("correct","different"):
        (configs/(ident+".yaml")).write_text("fixture: {}")
    (desktop/"installations.json").write_text(json.dumps([
        {"id":"different","installPath":str(tmp_path/"other")},
        {"id":"correct","installPath":cfg["source"]}]))
    monkeypatch.setenv("APPDATA",str(desktop.parent))
    settings=runtime.installation("video",cfg)
    assert settings["model_configs"]==[str(configs/"correct.yaml")]
    (desktop/"installations.json").unlink()
    model_home=Path(cfg["source"])/"models"
    (model_home/"vae").mkdir(parents=True)
    settings=runtime.installation("video",cfg)
    assert not settings["missing"] and not settings["model_configs"]
    base=runtime._base(tmp_path/"home","video")
    args=runtime._command(base,"video",settings)
    model_list=base/"studio-models.yaml"
    assert str(model_list) in args
    assert json.loads(model_list.read_text())["studio_models_0"]=={"base_path":str(model_home),"vae":"vae/"}


def test_editor_engine_controls_store_profiles_and_switch_only_to_owned_worker(tmp_path,monkeypatch):
    from h3studio.engine_controls import engine_action
    studio=SimpleNamespace(home=tmp_path,prefs_path=tmp_path/"preferences.json",
                           prefs={"video_url":"http://127.0.0.1:8010","unrelated":42})
    _,cfg=fake_install(tmp_path,"video")
    cfg["port"]=18021
    args={"kind":"video","profile":cfg,"connections":{"director_model":"selected-qwen"}}
    engine_action(studio,"engine_config",args)
    assert studio.prefs["unrelated"]==42
    assert studio.prefs["engine_profiles"]["video"]==cfg
    occupied={"owned":False,"state":"occupied","url":"http://127.0.0.1:18021"}
    monkeypatch.setattr(runtime,"start_engine",lambda home,kind,cfg:occupied)
    assert engine_action(studio,"start_engine",{"kind":"video"})==occupied
    assert studio.prefs["video_url"].endswith("8010")
    owned={**occupied,"owned":True,"state":"starting"}
    monkeypatch.setattr(runtime,"start_engine",lambda home,kind,cfg:owned)
    assert engine_action(studio,"start_engine",{"kind":"video"})==owned
    assert json.loads(studio.prefs_path.read_text())["video_url"]==owned["url"]
    before=studio.prefs_path.read_bytes()
    with pytest.raises(ValueError,match="different port"):
        engine_action(studio,"engine_config",{"kind":"image","profile":{"port":18021}})
    assert studio.prefs_path.read_bytes()==before
