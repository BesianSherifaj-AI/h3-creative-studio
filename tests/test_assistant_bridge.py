"""A separate stdio client talks to the real desktop transport and persisted project."""
from contextlib import contextmanager
import http.cookiejar
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

import pytest

from h3studio import director, media
from h3studio.application import Studio
from h3studio.project import Project
from h3studio.server import Server


@pytest.fixture
def desktop(tmp_path):
    p = Project.create(tmp_path / "project", "Relay")
    shots = [p.add_shot(label, 3) for label in ("Watch", "Receiver", "Shore")]
    director.seed_example(p, shots)
    studio = Studio(tmp_path / "home", p.folder)
    server = Server(studio)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    with browser.open(server.url):
        pass

    def command(op, **fields):
        call = urllib.request.Request(server.origin + "/api", json.dumps({"op": op, **fields}).encode(),
                                      {"Content-Type": "application/json", "Origin": server.origin})
        with browser.open(call) as result:
            return json.load(result)

    try:
        yield server, command, p
    finally:
        server.shutdown()
        server.server_close()
        studio.close()
        thread.join(timeout=3)


@contextmanager
def client(info):
    spec = info["config"]["mcpServers"]["h3-studio"]
    with subprocess.Popen([spec["command"], *spec["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        index = 0

        def rpc(method, params=None, reply=True):
            nonlocal index
            msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
            if reply:
                index += 1
                msg["id"] = index
            process.stdin.write(json.dumps(msg).encode() + b"\n")
            process.stdin.flush()
            if reply:
                response = json.loads(process.stdout.readline())
                assert response["id"] == index
                return response

        def call(name, **args):
            result = rpc("tools/call", {"name": name, "arguments": args})["result"]
            if result["isError"]:
                raise ValueError(result["content"][0]["text"])
            assert result["structuredContent"] == json.loads(result["content"][0]["text"])
            return result["structuredContent"]

        try:
            assert rpc("tools/list")["error"]["code"] == -32002
            result = rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                        "clientInfo": {"name": "test-editor", "version": "1"}})
            assert result["result"]["capabilities"] == {"tools": {}}
            assert result["result"]["protocolVersion"] == "2025-11-25"
            rpc("notifications/initialized", reply=False)
            yield rpc, call
        finally:
            process.stdin.close()
            assert process.wait(timeout=10) == 0
            assert not process.stderr.read()


def test_pair_lifecycle_and_project_isolation(desktop, tmp_path):
    server, ui, project = desktop
    assert not ui("assistant_status")["active"]
    info = ui("assistant_enable", scope="read")
    file = Path(info["config"]["mcpServers"]["h3-studio"]["args"][-1])
    with client(info) as (rpc, call):
        names = [x["name"] for x in rpc("tools/list")["result"]["tools"]]
        assert "game_world" in names
        assert rpc("tools/call", {"name": "unsupported_tool"})["error"]["code"] == -32602
        assert not any(x.startswith("propose") for x in names)
        assert call("project_overview", limit=1)["cut"]["next_offset"] == 1
        cut = call("project_overview", view="timeline", offset=1, limit=1)
        assert cut["frames"] == 216 and cut["cut"]["items"][0]["start_frame"] == 72
        assert not cut["cut"]["items"][0]["media_ready"]
        with pytest.raises(ValueError, match="reviewable"):
            call("propose_film", request_id="one", title="Draft", shots=[dict(title="Shot", prompt="Wide", duration=3)])
        # A different site cannot use a leaked browser session or the stdio bearer as a UI cookie.
        saved = json.loads(file.read_text())
        assert saved["port"] == server.server_port
        for path, headers in (("/assistant", {"Authorization": "Bearer " + saved["token"], "Origin": "https://example.org"}),
                              ("/api", {"Authorization": "Bearer " + saved["token"]})):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(urllib.request.Request(server.origin + path, b'{"op":"state"}', headers))
            assert e.value.code == 403
        ui("assistant_revoke")
        assert not file.exists()
        info_again = ui("assistant_enable", scope="propose")
        assert info_again["config"] != info["config"]
        with pytest.raises(ValueError, match="Pair this client"):
            call("project_overview")
    with client(info_again) as (_, call):
        assert call("project_overview")["project"] == project.state()["id"]
        ui("new", path=str(tmp_path / "other"), name="Other project")
        with pytest.raises(ValueError, match="expired|revoked"):
            call("project_overview")
        assert not ui("assistant_status")["active"]
    expiring = ui("assistant_enable")
    with client(expiring) as (_, call):
        server.assistant.expires = time.time() - 1
        with pytest.raises(ValueError, match="expired"):
            call("project_overview")


def test_game_board_motion_and_voice_review_workflows(desktop):
    server, ui, project = desktop
    with client(ui("assistant_enable", scope="propose")) as (rpc, call):
        tools = rpc("tools/list")["result"]["tools"]
        assert all(t["inputSchema"]["additionalProperties"] is False for t in tools)
        overview = call("project_overview")
        root = overview["active_game_turn"]
        first = overview["cut"]["items"][0]["id"]
        world = call("game_world", turn=root)
        assert world["scene"] and world["story"]["choices"] and world["context"]["actor"]["name"]
        choice = world["story"]["choices"][0]
        ticket = call("propose_game_choice", request_id="play", turn=root, choice=choice["id"])
        assert call("game_branches")["total"] == 1
        decision = dict(project=overview["project"], project_folder=str(project.folder), proposal=ticket["id"], decision="approve")
        with pytest.raises(urllib.error.HTTPError):
            ui("assistant_decide", **dict(decision, project_folder=str(project.folder.parent)))
        resolved = ui("assistant_decide", **decision)
        assert resolved == ui("assistant_decide", **decision)
        assert resolved == {"status": "approved", "result": {"turn": call("game_branches")["active"]}}
        assert call("game_branches")["total"] == 2
        assert call("proposal_status", proposal=ticket["id"])["result"]["turn"] == call("game_branches")["active"]

        action = next(a for a in world["actions"]["actions"] if a["kind"] == "take" and a["enabled"])
        args = dict(request_id="lamp", parent=root, world_revision=world["revision"], action=action["label"],
                    control={"kind": action["kind"], "target_id": action["target_id"]},
                    scene="The lamp catches the light.", choices=["Follow the beacon", "Return to the receiver"])
        result = call("propose_game_turn", **args)
        assert result == call("propose_game_turn", **args)
        assert call("game_branches")["total"] == 2
        with pytest.raises(ValueError, match="different"):
            call("propose_game_turn", **dict(args, scene="Another scene"))
        turn = ui("director_approve", review=result["id"])["turn"]
        assert call("proposal_status", proposal=result["id"])["status"] == "approved"
        assert call("game_world", turn=turn)["inventory"][0]["id"] == action["target_id"]
        assert not call("game_world", turn=root)["inventory"]
        board = call("propose_film", request_id="board", title="Breakwater",
                     shots=[dict(title="Lookout", duration=3, prompt="A light passes along the wall.")])
        assert call("project_overview")["cut"]["total"] == 3
        ui("director_approve", review=board["id"])
        assert call("film_boards", board=board["id"])["rows"]["items"][0]["shot"]
        assert call("project_overview")["cut"]["total"] == 4

        revision = project.save_composition("Painted frame", 64, 64, [])
        assert call("image_library")["items"][0]["id"] == revision
        ui("recipe_draft", shot=first, recipe={"prompt": "Original", "seed": 17, "settings": {}}, start={"mode": "visual"})
        draft_args = dict(shot=first, prompt="Pan to the painted anchor.", guide={"frame": 32, "revision": revision})
        stale = call("propose_shot_draft", request_id="stale", **draft_args)
        ui("recipe_draft", shot=first, recipe={"prompt": "Author's change", "seed": 19})
        with pytest.raises(urllib.error.HTTPError):
            ui("assistant_decide", project=overview["project"], proposal=stale["id"], decision="approve")
        ui("assistant_decide", project=overview["project"], proposal=stale["id"], decision="dismiss")
        draft = call("propose_shot_draft", request_id="paint", **draft_args)
        ui("assistant_decide", project=overview["project"], proposal=draft["id"], decision="approve")
        saved = call("shot_inspect", shot=first)
        assert saved["seed"] == 19 and saved["guides"][0]["revision"] == revision
        assert saved["prompt"] == draft_args["prompt"]
        with pytest.raises(ValueError):
            call("propose_shot_draft", request_id="file", shot=first, prompt="Unrelated", path="local.txt")
        voice = call("propose_voice_script", request_id="speak", title="Coast", text="Harbour light.", speaker="Keeper", turn=turn)
        ui("assistant_decide", project=overview["project"], proposal=voice["id"], decision="approve")
        assert ui("assistant_status")["scripts"][0]["turn"] == turn
        assert Project(project.folder).state()["assistant_proposals"][draft["id"]]["status"] == "approved"
        opening = call("propose_game_turn", request_id="new-story", setup={"name": "Lyra", "location": "The Archive"},
                       action="Begin the night watch", scene="Paper stirs.", choices=["Inspect the map"])
        ui("director_approve", review=opening["id"])
        fresh = call("game_world", turn=opening["id"])
        assert fresh["parent"] is None and fresh["context"]["actor"]["name"] == "Lyra"
        assert fresh["context"]["location"]["name"] == "The Archive"
        assert server.studio.queue.list() == []


def test_default_overview_and_shot_inspection_follow_trimmed_split_cut(desktop, tmp_path, monkeypatch):
    _, ui, _ = desktop
    ui("new", path=str(tmp_path / "split-film"), name="After the cut")
    source = tmp_path / "three seconds.mp4"
    source.write_bytes(b"synthetic 72-frame source")
    metadata = dict(duration=3, duration_exact="3", fps=24, width=96, height=64, pix_fmt="yuv420p", audio_streams=[])
    monkeypatch.setattr(media, "probe_media", lambda path: metadata.copy())
    head, other = [ui("import_clip", path=str(source))["shot"] for _ in range(2)]
    def edit(op, **values):
        return ui(op, version=ui("cut_view")["version"], **values)
    edit("cut_trim", shot=head, start=0.5, end=1.5)
    edit("cut_trim", shot=other, start=1, end=2)
    tail = edit("cut_split", shot=head, frame=12)["shot"]
    with client(ui("assistant_enable")) as (_, call):
        overview = call("project_overview")
        assert overview["frames"] == 48
        expected = [(head, 0, 12, .5, 1, 3), (tail, 12, 24, 1, 1.5, .5), (other, 24, 48, 1, 2, 3)]
        for row, (shot, first, last, start, end, nominal) in zip(overview["cut"]["items"], expected, strict=True):
            detail = call("shot_inspect", shot=shot)
            for record in (row, detail):
                assert record["id"] == shot and record["media_ready"]
                assert (record["start_frame"], record["end_frame"]) == (first, last)
                assert (record["source_in"], record["source_out"]) == (start, end)
                assert record["nominal_duration"] == nominal
                assert record["delivered_duration"] == record["duration"] == (last - first) / 24


@pytest.mark.skipif(not shutil.which("node"), reason="Needs Node for the desktop handler fixture")
def test_game_choice_review_opens_returned_child_and_dismiss_stays_in_inbox():
    script = r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const directorSource = fs.readFileSync(process.argv[2], 'utf8');
async function check(dismiss, switchFolder=false) {
  const info = {project:'cut-id', folder:'one', pending:[{id:'pitch-id', kind:'propose_game_choice', title:'Shore'}], scripts:[]};
  const button = {dataset:dismiss ? {aiDismiss:'pitch-id'} : {aiReview:'pitch-id'}};
  const parent={action:'Ready', scene:'Waiting by the relay.', state_changes:[], actions:['Follow the shore']};
  const child={action:'Walk to shore', scene:'Light catches the breaker.', parent:'parent', state_changes:[], actions:[]};
  const director={turns:{parent},active_turn:'parent',boards:{},reviews:{}};
  const state = {doc:{id:'cut-id',director,shots:{},takes:{},assets:{},revisions:{}},folder:'one',preferences:{}};
  const workspace = {querySelector:()=>null};
  const decisions = [], visits = [];
  let opens = 0;
  const globals = {
    window:{}, state, get:()=>state, closeModal:()=>{}, setInterval:()=>0, clearInterval:()=>{}, toast:()=>{},
    document:{querySelector:s=>s==='#directorWorkspace'?workspace:null,querySelectorAll:s=>s.includes('review') ? [button] : []},
    esc:String, sel:()=>'', modal:()=>{ opens++; }, on:()=>{}, handle:fn=>fn,
    refresh:async()=>{ if (switchFolder) state.folder = 'other'; },
    api:async(op, args)=>{
      if (op==='assistant_status') return info;
      if (op==='director_inbox') return [];
      if (op==='director_story_view') {
        visits.push([args.turn,args.project_id,args.project_folder]);
        return {turn:args.turn,title:director.turns[args.turn].action,visual:{},choices:[],waypoints:[],inventory:[],objectives:[]};
      }
      assert.equal(op, 'assistant_decide');
      decisions.push(args);
      if (!dismiss) {director.turns['actual-child']=child;director.active_turn='actual-child';}
      return dismiss ? {status:'dismissed',result:null} : {status:'approved',result:{turn:'actual-child'}};
    }
  };
  vm.createContext(globals);
  vm.runInContext(directorSource,globals);
  globals.window.StudioShell={selectRoom:room=>globals.window.DirectorRoom.open(room)};
  vm.runInContext(source,globals);
  await globals.window.DirectorRoom.playTurn('parent','cut-id','one');
  assert.ok(workspace.innerHTML.includes('class="dr-play-narration">'+parent.scene));
  visits.length=0;
  await globals.window.StudioAssistant.open();
  if (switchFolder) await assert.rejects(button.onclick(), /Reopen AI clients/);
  else await button.onclick();
  assert.equal(button.disabled,false);
  assert.equal(decisions.length,1);
  assert.equal(decisions[0].proposal,'pitch-id');
  assert.equal(decisions[0].project_folder,'one');
  assert.equal(decisions[0].decision,dismiss ? 'dismiss' : 'approve');
  assert.deepEqual(visits, dismiss||switchFolder ? [] : [['actual-child','cut-id','one']]);
  assert.ok(workspace.innerHTML.includes('class="dr-play-narration">'+(dismiss||switchFolder ? parent.scene : child.scene)));
  assert.equal(opens, dismiss ? 2 : 1);
}
(async()=>{ await check(false); await check(true); await check(false,true); })().catch(e=>{console.error(e);process.exitCode=1;});
"""
    web = Path(__file__).resolve().parents[1] / "h3studio/web"
    subprocess.run(["node", "-e", script, str(web / "assistant-bridge.js"), str(web / "director-room.js")], check=True, timeout=10)
