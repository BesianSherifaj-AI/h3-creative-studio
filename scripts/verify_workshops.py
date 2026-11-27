"""Exercise the community workshops in a dedicated native window and decode its output."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time

import av
from playwright.sync_api import sync_playwright

from verify_community import close_window, pick_path


def assistant_call(config, name, **arguments):
    init = {"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"Studio tour","version":"1.0"}}
    calls = [(1,"initialize",init),(None,"notifications/initialized",{}),(2,"tools/list",{}),
             (3,"tools/call",{"name":name,"arguments":arguments})]
    requests = [{"jsonrpc":"2.0","method":m,"params":a,**({"id":i} if i is not None else {})} for i,m,a in calls]
    result = subprocess.run([config["command"],*config["args"]],text=True,encoding="utf-8",timeout=30,
                            input="".join(json.dumps(r)+"\n" for r in requests),capture_output=True,check=True)
    replies = {r["id"]:r for r in map(json.loads,result.stdout.splitlines())}
    assert len(replies[2]["result"]["tools"]) >= 9
    result = replies[3]["result"]
    assert not result.get("isError"), result
    return result["structuredContent"]


def exercise(page, folder, record=False):
    check = page.wait_for_function
    def snap(name):
        page.wait_for_timeout(220)
        page.screenshot(path=folder/(name+".jpg"),type="jpeg",quality=96,animations="disabled")
    def record_player(name, selector, play=None):
        if not record:
            return
        player=page.locator(selector)
        page.wait_for_function("s=>document.querySelector(s)?.readyState >= 2",arg=selector,timeout=40000)
        if play:
            page.click(play)
        else:
            player.evaluate("v=>v.play()")
        group=folder/("record-"+name)
        group.mkdir()
        first=time.monotonic()
        for i in range(17):
            delay=first+i/8-time.monotonic()
            if delay>0:
                page.wait_for_timeout(delay*1000)
            page.screenshot(path=group/f"{i:03}.jpg",type="jpeg",quality=89)
        if play:
            stop=page.locator(play)
            if "Pause" in stop.inner_text():
                stop.click()
        else:
            player.evaluate("v=>v.pause()")
    def button(label):
        return page.get_by_role("button",name=label,exact=True)
    def lesson(name):
        if page.locator("#dialog").evaluate("(d)=>d.open"):
            page.click("#closeDialog")
        page.click("#openStarter")
        page.click('[data-lesson="'+name+'"]')
        check("name=>H3.get().doc.name==='Tidal Relay - '+name",arg={"assistant":"AI clients"}.get(name,name.title()))
    check("window.H3?.get().doc?.name==='Tidal Relay' && typeof window.pywebview?.api?.choose==='function'")
    page.set_viewport_size({"width":1440,"height":900})
    page.click("#openStarter")
    check("[...document.querySelectorAll('.lesson-card img')].length===8 && [...document.querySelectorAll('.lesson-card img')].every(i=>i.complete&&i.naturalWidth>0)")
    snap("examples")
    page.click("#closeDialog")
    lesson("game")
    page.wait_for_selector('[data-dr=storyChoice][data-v=shore]')
    record_player("game-opening","#drStoryVideo")
    snap("game-opening")
    page.click('[data-dr=storyChoice][data-v=shore]')
    page.wait_for_selector('[data-dr=storyChoice][data-v=beacon]')
    record_player("game-shore","#drStoryVideo")
    snap("game-shore")
    page.click('[data-dr=bookmark]')
    page.wait_for_selector('[data-dr=restorePoint]')
    page.click('[data-dr=storyChoice][data-v=beacon]')
    check("document.querySelector('.dr-player h2')?.textContent==='A light to steer by'")
    snap("game-ending")
    page.click('[data-dr=storyFilm]')
    check("document.body.dataset.room==='film'")
    snap("branch-board")
    lesson("image")
    page.click('[data-layer="2"]');snap("image-title")
    orig=page.evaluate("H3.get().rev.id")
    page.fill("#lx","360");page.click("#applyLayer")
    check("old=>H3.get().rev.id!==old && H3.get().rev.layers[2].x===360",arg=orig)
    snap("image-edited")
    lesson("motion")
    page.wait_for_selector('.motion-guides img')
    snap("motion")
    for name in ("First","Middle","Last"):
        page.locator(".motion-guides button",has_text=name).click()
        page.wait_for_selector('#dialog img');snap("guide-"+name.lower());page.click('#closeDialog')
    lesson("film");page.click('[data-dr=row][data-v="1"]')
    page.click("[data-dr=lock]");page.wait_for_selector('[data-dr=lock]:has-text("Unlock")')
    snap("film-lock")
    page.click('[data-dr=timeline]');page.wait_for_selector('#cutPicture .chosen')
    check("document.querySelector('#cutProperties h3')?.textContent==='The receiver wakes'")
    check("document.querySelector('#cutVideo')?.readyState>=2")
    snap("edit")
    record_player("edit","#cutVideo","#cutPlay")
    lesson("voice")
    page.wait_for_selector('#voiceName')
    page.fill('#voiceText','Across the water, a light answers.')
    options=page.locator("#voiceName option").all_text_contents()
    page.select_option("#voiceName",label=next((label for label in options if "en-" in label),options[0]))
    snap("voice-script")
    button("Generate voice").click()
    page.wait_for_selector("#voiceAudio",timeout=120000)
    check("document.querySelector('#voiceAudio')?.duration>0")
    page.locator("#voiceAudio").evaluate("a=>a.play()")
    check("document.querySelector('#voiceAudio').currentTime>.15")
    snap("voice-listen")
    record_player("voice-listen","#voiceAudio")
    page.fill('#voiceOffset','1');page.fill('#voiceTrack','2')
    button("Add to timeline").click()
    check("Object.keys(H3.get().doc.soundtracks).some(k=>k.startsWith('voice-'))")
    page.click('#closeDialog');page.wait_for_selector('#cutBeds svg')
    snap("voice-mix")
    page.click('#export');assert page.input_value('#ea')=='2'
    page.select_option('#deliveryPreset','vertical')
    page.wait_for_selector('#deliveryMask video:not([hidden])')
    check("document.querySelector('#deliveryPreview').videoWidth>0 && document.querySelector('#deliveryPreview').style.objectFit==='cover'")
    snap("portrait")
    bounds = page.locator("#deliveryMask").bounding_box()
    cx, cy = bounds["x"]+bounds["width"]/2, bounds["y"]+bounds["height"]/2
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move(cx+30, cy)
    page.mouse.up()
    check("Number(document.querySelector('#deliveryX').value)<50")
    page.click("#deliveryCrop summary")
    page.locator("#deliveryX").press("End")
    check("document.querySelector('#deliveryPreview').style.objectPosition==='100% 50%'")
    page.click("#resetCrop")
    check("document.querySelector('#deliveryPreview').style.objectPosition==='50% 50%'")
    # Render a modest companion fixture through the same UI. Preserve the chosen 9:16 framing.
    page.fill('#ew','360');page.fill('#eh','640')
    path=folder/'Narrated vertical.mp4';pick_path(page,path)
    button("Choose export file").click()
    deadline=time.monotonic()+120
    while time.monotonic()<deadline:
        job=page.evaluate("(async()=>(await H3.api('jobs')).editorial.find(x=>x.name==='Export film'))()")
        if job and job["state"] in ("completed","cancelled","failed"):
            break
        time.sleep(.25)
    assert job and job["state"]=="completed", job
    with av.open(str(path)) as c:
        v=c.streams.video[0]; assert (v.width,v.height)==(360,640)
        assert c.streams.subtitles[0].codec_context.name=="mov_text"
        count=sum(1 for _ in c.decode(video=0));assert count==240
    with av.open(str(path)) as c:
        assert len(c.streams.audio)==2
        assert sum(f.samples for f in c.decode(audio=0)) >= 480000
    with av.open(str(path)) as c:
        import numpy as np
        speech=np.concatenate([frame.to_ndarray()[0] for frame in c.decode(audio=1)])
        assert np.max(np.abs(speech[4800:24000]))<.001 and np.max(np.abs(speech[50400:120000]))>.02
    with av.open(str(path)) as c:
        assert any(b'light answers' in bytes(p) for p in c.demux(subtitles=0))
    lesson("assistant");page.wait_for_selector('#aiScope')
    snap("assistant-pair")
    page.select_option('#aiScope','propose');button("Enable connection").click()
    page.wait_for_selector('#aiClientConfig')
    cfg=json.loads(page.input_value('#aiClientConfig'))['mcpServers']['h3-studio']
    shot=page.evaluate("H3.get().shot.id")
    pitch="Track slowly toward the listening station as the lamp answers across the water."
    assistant_call(cfg,"propose_shot_draft",request_id="tour-motion",shot=shot,prompt=pitch)
    button("Refresh inbox").click();page.wait_for_selector('[data-ai-review]')
    # Disconnect before capturing the inbox so the local install paths never appear in public footage.
    button("Disconnect client").click();page.wait_for_selector('#aiScope')
    snap("assistant-inbox")
    page.click('[data-ai-review]');check("document.querySelectorAll('[data-ai-review]').length===0")
    page.click('#closeDialog')
    check("pitch=>document.querySelector('#motionPrompt')?.value===pitch",arg=pitch)
    snap("assistant-applied")

    page.click('[data-room-select="game"]')
    page.click('[data-dr="playScene"]')
    page.wait_for_selector(".dr-play-narration")
    page.click("#help");page.click("#setupAssistant")
    page.select_option("#aiScope","propose");button("Enable connection").click()
    cfg=json.loads(page.input_value("#aiClientConfig"))["mcpServers"]["h3-studio"]
    old=assistant_call(cfg,"game_world")
    assert page.locator(".dr-play-narration").text_content()==old["scene"]
    choice=old["story"]["choices"][0]
    proposal=assistant_call(cfg,"propose_game_choice",request_id="tour-game-choice",turn=old["turn"],choice=choice["id"])
    button("Refresh inbox").click();page.click('[data-ai-review="'+proposal["id"]+'"]')
    check("document.body.dataset.room==='game' && !document.querySelector('#dialog').open")
    accepted=assistant_call(cfg,"proposal_status",proposal=proposal["id"])
    assert accepted["status"]=="approved"
    child=assistant_call(cfg,"game_world",turn=accepted["result"]["turn"])
    assert child["parent"]==old["turn"] and child["story"]["node"]==choice["id"] and child["scene"]!=old["scene"]
    check("sc=>document.querySelector('.dr-play-narration')?.textContent===sc",arg=child["scene"])
    assert page.locator(".dr-player [data-dr=storyChoice]").evaluate_all("buttons=>buttons.map(b=>b.dataset.v)")==[
        entry["id"] for entry in child["story"]["choices"]]
    snap("assistant-game-child")
    return {"video":str(path),"frames":count,"size":[360,640],"subtitle":"mov_text",
            "game_ending":"A light to steer by","game_board":True,"narration_audition":True,
            "assistant_game_player_child":{"parent":old["turn"],"choice":choice["id"],"turn":child["turn"],
                                           "scene":child["scene"]},
            "assistant_subprocess_reviewed":True,"image_title_x":360}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe",type=Path)
    parser.add_argument("--record",action="store_true",help="Capture the native players for the release tour.")
    parser.add_argument("--evidence",type=Path,required=True)
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    args.evidence.mkdir(exist_ok=True,parents=True)
    folder=Path(tempfile.mkdtemp(prefix="Workshops-",dir=args.evidence)).resolve()
    print(folder,flush=True)
    with socket.socket() as s:
        s.bind(("127.0.0.1",0));port=s.getsockname()[1]
    env={**os.environ,"WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS":f"--remote-debugging-port={port}"}
    cmd=[str(args.exe.resolve())] if args.exe else [str(root/".venv/Scripts/pythonw.exe"),str(root/"studio.py")]
    subprocess.Popen([*cmd,"--home",str(folder/"home")],cwd=folder,env=env)
    try:
        with sync_playwright() as pw:
            for _ in range(100):
                try:
                    browser=pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}",timeout=900)
                    page=browser.contexts[0].pages[0]
                    break
                except Exception:
                    time.sleep(.25)
            else:
                raise RuntimeError("Studio did not open a native test window.")
            errors=[]
            page.on("pageerror",lambda error:errors.append(str(error)))
            page.set_default_timeout(15000)
            try:
                receipt=exercise(page,folder,args.record)
                assert not errors,errors
            except Exception:
                page.screenshot(path=folder/"failure.jpg")
                print("Room, dialog, error:",page.evaluate("[document.body.dataset.room,document.querySelector('#dialogTitle')?.textContent,document.querySelector('#dialogError')?.textContent]"),errors,flush=True)
                raise
            (folder/"verified.json").write_text(json.dumps(receipt,indent=2),encoding="utf-8")
            print(json.dumps(receipt),flush=True)
    finally:
        close_window(port)


if __name__=="__main__":
    main()
