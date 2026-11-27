"""Exercise source-relative edits, mixed-cadence playback and native timeline controls."""
import base64
import io
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

import numpy as np
from PIL import Image
import pytest

from h3studio.application import Studio
from h3studio.project import Project

pytestmark=pytest.mark.skipif(not shutil.which("ffmpeg"),reason="FFmpeg required")


def two_shots(folder):
    project=Project.create(folder/"film","Cut room check")
    takes=[]
    for index,(name,fps,tone) in enumerate((("red",24,440),("blue",60,880))):
        path=folder/(name+".mp4")
        subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-f","lavfi","-i",f"color={name}:s=128x72:r={fps}:d=3",
                        "-f","lavfi","-i",f"sine=f={tone}:r=48000:d=3","-af","volume=0:enable='lt(t,0.5)'",
                        "-map","0:v","-map","1:a","-c:v","libx264","-pix_fmt","yuv420p","-c:a","aac",str(path)],check=True)
        shot=project.add_shot(name.title(),3)
        take=project.save_take(shot,{"kind":"import","audio_route":[0,None],"delivery_trim":[index*.5,2.5]},project.add_asset(path))
        takes.append((shot,take))
    return project,takes


def test_immutable_split_trim_reorder_remove_undo_and_locks(tmp_path):
    project,((first,original),(second,_))=two_shots(tmp_path)
    studio=Studio(tmp_path/"settings",project.folder)
    try:
        def view():return studio.action("cut_view",{})
        def edit(operation,**values):return studio.action("cut_"+operation,dict(version=view()["version"],**values))
        initial=view()
        assert [c["end_frame"] for c in initial["clips"]]==[60,108]
        tail=edit("split",shot=second,frame=84)["shot"]
        doc=project.state()
        assert doc["cut"]==[first,second,tail]
        head_take,tail_take=[doc["takes"][doc["shots"][s]["selected_take"]] for s in (second,tail)]
        assert head_take["settings"]["delivery_trim"]==[.5,1.5] and tail_take["settings"]["delivery_trim"]==[1.5,2.5]
        assert head_take["video_asset"]==tail_take["video_asset"] and head_take["parent_take"]==tail_take["parent_take"]
        assert doc["takes"][head_take["parent_take"]]["settings"]["delivery_trim"]==[.5,2.5]
        with pytest.raises(ValueError,match="changed"):
            studio.action("cut_remove",{"version":initial["version"],"shot":first})
        edit("trim",shot=first,start=.25,end=1.25)
        assert view()["frames"]==72 and project.state()["takes"][original]["settings"]["delivery_trim"]==[0,2.5]
        edit("reorder",shot=tail,order=[tail,first,second])
        edit("remove",shot=second)
        assert project.state()["cut"]==[tail,first]
        assert view()["undo"]=="Remove shot"
        edit("undo");edit("undo")
        assert project.state()["cut"]==[first,second,tail]
        edit("redo")
        assert Project(project.folder).state()["cut"]==[tail,first,second]
        with project.transaction() as doc:doc["shots"][tail]["locked_take"]=tail_take["id"]
        with pytest.raises(ValueError,match="Unlock"):edit("remove",shot=tail)
        with pytest.raises(ValueError,match="Unlock"):edit("trim",shot=tail,start=1,end=2.5)
        edit("undo")
        edit("undo")
        assert view()["undo"]=="Split shot"
        with pytest.raises(ValueError,match="Unlock"):edit("undo")
        assert project.state()["shots"][tail]["selected_take"]==tail_take["id"]
    finally:studio.close()


def test_real_thumbnails_waveforms_and_decoded_program_audio(tmp_path):
    project,((_,red),(_,blue))=two_shots(tmp_path)
    studio=Studio(tmp_path/"settings",project.folder)
    try:
        token=studio.action("cut_view",{})["version"]
        def call(op,**args):return studio.action("cut_"+op,dict(version=token,**args))
        stripe=call("strip",take=red)
        assert len(stripe["images"])==3 and len(stripe["peaks"])==160
        assert max(stripe["peaks"][40:])>.09 and max(stripe["peaks"][:20])<.005
        blue_stripe=call("strip",take=blue)
        for strip,index in ((stripe,0),(blue_stripe,2)):
            image=Image.open(io.BytesIO(base64.b64decode(strip["images"][1].split(",")[1])))
            assert image.size==(128,72) and image.getpixel((64,36))[index]>230
        assert not any(call("strip",take=red,track=1)["peaks"])
        task=call("preview")["task"]
        duplicate=call("preview")
        assert duplicate.get("task")==task or "asset" in duplicate
        deadline=time.monotonic()+40
        while studio.tasks[task]["state"] in ("running","queued") and time.monotonic()<deadline:time.sleep(.05)
        result=studio.tasks[task]
        assert result["state"]=="completed",result
        asset=result["result"]["asset"]
        assert call("preview")["asset"]==asset
        path=project.asset_path(asset)
        def decode(args):
            return subprocess.check_output(["ffmpeg","-hide_banner","-loglevel","error","-i",str(path),*args,"pipe:1"])
        rgb=np.frombuffer(decode(["-map","0:v","-vf","scale=1:1","-f","rawvideo","-pix_fmt","rgb24"]),dtype=np.uint8).reshape(-1,3)
        assert len(rgb)==108 and np.all(rgb[:60,0]>220) and np.all(rgb[60:,2]>220)
        samples=np.frombuffer(decode(["-map","0:a","-ac","1","-ar","48000","-f","f32le"]),dtype="<f4")
        for at,tone in ((1,440),(3,880)):
            chunk=samples[at*48000:at*48000+8192]
            dominant=np.argmax(np.abs(np.fft.rfft(chunk*np.hanning(len(chunk)))))*48000/len(chunk)
            assert abs(dominant-tone)<7
    finally:studio.close()
def test_monitor_alternate_soundtrack_bus(tmp_path):
    project,((_,red),_)=two_shots(tmp_path)
    studio=Studio(tmp_path/"settings",project.folder)
    try:
        # Output two is explicitly silent on the clips. Place one second of score on that bus only.
        bed=tmp_path/"score.wav"
        subprocess.run(["ffmpeg","-loglevel","error","-f","lavfi","-i","sine=f=660:r=48000:d=1","-c:a","pcm_s16le",str(bed)],check=True)
        studio.action("soundtrack_save",dict(path=str(bed),track=1,stream=0,offset=1.5,gain_db=-6))
        view=studio.action("cut_view",{})
        assert view["audio_tracks"]==2 and view["beds"][0]["offset"]==1.5
        def call(op,**args):return studio.action("cut_"+op,dict(version=view["version"],track=1,**args))
        bed_strip=call("strip",bed=view["beds"][0]["id"])
        assert bed_strip["duration"]==1 and .055<max(bed_strip["peaks"])<.07
        assert not any(call("strip",take=red)["peaks"])
        job=studio.tasks[call("preview")["task"]]
        deadline=time.monotonic()+30
        while job["state"] in ("running","queued") and time.monotonic()<deadline:time.sleep(.05)
        assert job["state"]=="completed",job
        path=project.asset_path(job["result"]["asset"])
        audio=np.frombuffer(subprocess.check_output(["ffmpeg","-loglevel","error","-i",str(path),"-vn","-ac","1","-ar","48000","-f","f32le","pipe:1"]),dtype="<f4")
        assert np.max(abs(audio[:48000]))<.0001 and np.max(abs(audio[3*48000:]))<.0001
        sample=audio[2*48000:2*48000+8192];tone=np.argmax(abs(np.fft.rfft(sample*np.hanning(8192))))*48000/8192
        assert abs(tone-660)<5
    finally:studio.close()


@pytest.mark.skipif(os.name!="nt" or os.getenv("H3_NATIVE_UI_TESTS")!="1",
                    reason="Set H3_NATIVE_UI_TESTS=1 to launch an isolated Windows Studio window.")
def test_native_edit_room_playback_and_direct_manipulation(tmp_path):
    import json
    playwright=pytest.importorskip("playwright.sync_api")
    project,((first,_),(second,_))=two_shots(tmp_path)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1",0));port=listener.getsockname()[1]
    repo=Path(__file__).resolve().parents[1]
    local=repo/".venv"/"Scripts"/"pythonw.exe"
    python=local if local.is_file() else Path(sys.executable).with_name("pythonw.exe")
    env=dict(os.environ,WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=f"--remote-debugging-port={port} --disable-gpu --autoplay-policy=no-user-gesture-required")
    proc=subprocess.Popen([str(python),str(repo/"studio.py"),"--project",str(project.folder),"--home",str(tmp_path/"home")],env=env)
    receipt={}
    try:
        with playwright.sync_playwright() as pw:
            deadline=time.monotonic()+30
            while True:
                try:
                    browser=pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}",timeout=1300)
                    break
                except playwright.Error:
                    if time.monotonic()>deadline or proc.poll() is not None:raise
                    time.sleep(.25)
            page=browser.contexts[0].pages[0]
            page.wait_for_function("window.H3?.get().doc")
            page.locator('[data-room-select="edit"]').click()
            page.wait_for_selector("#cutPicture img")
            page.wait_for_function("document.querySelectorAll('#cutAudioLane svg').length===2")
            assert page.locator("#cutPicture img").count()==6
            def order():
                return page.locator('#cutPicture [data-cut-shot]').evaluate_all("(els)=>els.map(e=>e.dataset.cutShot)")
            def wait_count(count):
                page.wait_for_function("n=>document.querySelectorAll('#cutPicture [data-cut-shot]').length===n",arg=count)
            def ruler(frame):
                box=page.locator("#cutRuler").bounding_box()
                scale=float(page.locator("#cutZoom").input_value())
                page.mouse.click(box["x"]+frame*scale/24,box["y"]+box["height"]/2)
            def color():
                return page.eval_on_selector("#cutVideo","v=>{let c=document.createElement('canvas');c.width=c.height=1;let g=c.getContext('2d');g.drawImage(v,0,0,1,1);return Array.from(g.getImageData(0,0,1,1).data).slice(0,3);}")
            ruler(48);page.locator("#cutPlay").click()
            page.wait_for_function("!document.querySelector('#cutVideo').paused && document.querySelector('#cutVideo').currentTime>2.06")
            receipt["before_cut_rgb"]=color()
            page.wait_for_function("document.querySelector('#cutVideo').currentTime>2.75")
            receipt["after_cut_rgb"]=color()
            assert receipt["before_cut_rgb"][0]>220 and receipt["after_cut_rgb"][2]>220,receipt
            page.locator("#cutPlay").click()
            assert page.locator("#cutVideo").evaluate("e=>e.paused && !e.muted && e.volume>0")
            ruler(24);page.locator("#cutSplit").click();wait_count(3)
            tail=order()[1]
            receipt["split"]=order()
            assert order()==[first,tail,second]
            page.locator("#cutRemove").click();wait_count(2)
            assert order()==[first,second]
            page.locator("#cutUndo").click();wait_count(3)
            page.locator("#cutRedo").click();wait_count(2)
            page.locator("#cutUndo").click();wait_count(3)
            selector='#cutPicture [data-cut-shot="{}"]'
            page.locator(selector.format(second)).drag_to(page.locator(selector.format(first)),target_position={"x":12,"y":20})
            page.wait_for_function("id=>document.querySelector('#cutPicture [data-cut-shot]').dataset.cutShot===id",arg=second)
            receipt["reorder"]=order()
            # Change the out point by dragging the right trim handle, then set numerical source in.
            edge=page.locator(selector.format(second)+" [data-edge=out]").bounding_box()
            x=edge["x"]+edge["width"]/2;y=edge["y"]+edge["height"]/2
            page.mouse.move(x,y);page.mouse.down();page.mouse.move(x-20,y,steps=5);page.mouse.up()
            page.wait_for_function("Number(document.querySelector('#cutOut').value)===2.25")
            page.wait_for_function("document.querySelector('#cutUndo').title==='Undo Trim shot'")
            page.locator("#cutIn").fill("0.75");page.locator("#cutApply").click()
            page.wait_for_function("document.querySelector('#cutSummary').textContent.includes('00:04:00')")
            assert page.locator("#cutIn").input_value()=="0.75"
            page.locator("#cutAudio").select_option("1")
            page.wait_for_function("document.querySelector('#cutTrackLabel').textContent.startsWith('A2')")
            page.locator("#cutAudio").select_option("0")
            page.wait_for_function("document.querySelectorAll('#cutPicture img').length===9 && document.querySelector('#cutTrackLabel').textContent.startsWith('A1')")
            receipt["clock"]=page.locator("#cutSummary").inner_text()
            receipt["trim"]=[float(page.locator("#cutIn").input_value()),float(page.locator("#cutOut").input_value())]
            if destination:=os.getenv("H3_CUT_RECEIPT"):
                dest=Path(destination);dest.mkdir(parents=True,exist_ok=True)
                page.screenshot(path=str(dest/"edit-room.png"))
                (dest/"desktop.json").write_text(json.dumps(receipt,indent=2),"utf-8")
    finally:
        if proc.poll() is None:proc.terminate()
        proc.wait(timeout=8)
