"""Native review uses the production tracker and each sample's presentation interval."""
import hashlib
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest

from h3studio.editor import editor_action
from h3studio.playback import finish_comparison
from h3studio.project import Project

pytestmark=pytest.mark.skipif(not shutil.which("ffmpeg"),reason="FFmpeg required")


def review_fixture(root):
    source=root/"before.mp4"
    after=root/"after.mp4"
    mask=root/"mask.png"
    texture=np.random.default_rng(1203).integers(45,235,(10,10),dtype=np.uint8)
    frames=[]
    for i in range(72):
        x=8+2*(i%24)
        other=40-2*(i%16)
        pixels=np.full((64,96,3),35,dtype=np.uint8)
        pixels[25:35,other:other+10,:]=texture[::-1,:,None]
        pixels[16:26,x:x+10,:]=texture[:,:,None]
        for bit in range(8):
            pixels[:8,bit*10:bit*10+8,:]=255 if i & (1<<bit) else 0
        frames.append(pixels.tobytes())
    a=np.zeros((64,96),dtype=np.uint8);a[16:26,8:18]=255
    Image.fromarray(a).save(mask)
    ff=["ffmpeg","-hide_banner","-loglevel","error","-y"]
    subprocess.run(ff+["-f","rawvideo","-pix_fmt","rgb24","-s","96x64","-r","24","-i","pipe:0",
                       "-f","lavfi","-i","sine=frequency=220:duration=3","-map","0:v","-map","1:a",
                       "-c:v","libx264","-pix_fmt","yuv420p","-crf","8","-c:a","aac",str(source)],
                   input=b"".join(frames),check=True)
    stamp="fps=60,drawbox=x=0:y=0:w=80:h=8:t=fill:color=black"
    for bit in range(8):
        stamp+=f",drawbox=x={bit*10}:y=0:w=8:h=8:t=fill:color=white:enable='bitand(n,{1<<bit})'"
    subprocess.run(ff+["-i",str(source),"-vf",stamp,"-c:v","libx264","-pix_fmt","yuv420p",
                       "-crf","12","-c:a","copy",str(after)],check=True)
    p=Project.create(root/"film","Source review")
    shot=p.add_shot("Crossing",3)
    parent=p.save_take(shot,{"kind":"import"},p.add_asset(source))
    finish=p.save_take(shot,{"kind":"finish"},p.add_asset(after),parent_take=parent)
    editor_action(None,p,"edit_anchor",{"take":parent,"source_index":0,"state":"keyframe","path":mask})
    return p,parent,finish,source,after


def test_review_before_saving_and_mixed_rate_comparison(tmp_path):
    p,parent,finish,src,after=review_fixture(tmp_path)
    def inspect(ix):
        return editor_action(None,p,"edit_info",{"take":parent,"source_index":ix,"propagate_from":0,"preview":True})
    def play(ix,end):
        return editor_action(None,p,"edit_info",{"take":parent,"source_index":ix,"propagate_from":0,"play_end":end})["samples"]
    initial=play(0,18)
    assert [f["state"] for f in initial]==["keyframe"]+["tracked"]*7+["ambiguous"]
    assert all(f["overlay"].startswith("data:image/png;base64,") and f["index"]/24==f["start"] for f in initial)
    tracked=inspect(8)["tracking"]
    assert inspect(7)["tracking"]["state"]=="tracked"
    assert tracked["state"]=="ambiguous" and tracked["at"]==8
    with Image.open(p.asset_path(tracked["mask"])) as mask:
        assert mask.getbbox()==(24,16,34,26)
    payload={"take":parent,"source_index":8,"state":"keyframe","initial":"current","base_mask":tracked["mask"],
             "strokes":[{"show":False,"radius":.05,"points":[[.20,.39],[.39,.39]]}]}
    editor_action(None,p,"edit_anchor",payload)
    corrected=inspect(8)["tracking"]
    assert corrected["state"]=="keyframe" and corrected["mask"]!=tracked["mask"]
    assert [f["state"] for f in play(8,18)]==["keyframe"]+["tracked"]*10
    doc=p.state()
    job={"id":"a"*32,"status":"succeeded","kind":"finish","payload":{"parent_take":parent,"studio_project":doc["id"]},
         "receipt":{"result":{"output_path":str(after),"sha256":hashlib.sha256(after.read_bytes()).hexdigest()}}}
    stub=SimpleNamespace(queue=SimpleNamespace(get=lambda *args,**kwargs:job))
    a=finish_comparison(stub,p,{"job":job["id"]})
    b=finish_comparison(None,p,{"take":finish})
    assert a==b and len(p.state()["takes"])==2
    assert a["source"]["runs"]==[[0,0,1/24,72]]
    assert a["finished"]["runs"]==[[0,0,1/60,180]]
    assert a["source"]["url"]!=a["finished"]["url"] and a["duration"]==3
    trimmed=p.save_take(doc["takes"][finish]["shot"],{"kind":"finish","delivery_trim":[.1,.6]},
                        doc["takes"][finish]["video_asset"],parent_take=finish)
    cut=finish_comparison(None,p,{"take":trimmed})
    assert cut["parent"]==parent and cut["duration"]==.5
    assert cut["source"]["runs"][0][0]==2 and cut["finished"]["runs"][0][0]==6
    job["payload"]["studio_project"]="other"
    with pytest.raises(ValueError,match="owned"):finish_comparison(stub,p,{"job":job["id"]})
    editor_action(None,p,"edit_anchor",{"take":parent,"source_index":18,"state":"hidden"})
    args={"take":parent,"source_index":18,"propagate_from":18,"play_end":71}
    first=editor_action(None,p,"edit_info",args)
    assert len(first["samples"])==48 and first["resume"]["state"]=="hidden"
    rest=editor_action(None,p,"edit_info",{**args,"source_index":66,"resume":first["resume"]})
    assert [s["index"] for s in rest["samples"]]==list(range(66,72)) and rest["resume"] is None


def verify_native_hold(page):
    """Read both visible bit stamps directly from the paused review images."""
    page.wait_for_function("document.querySelector('#compareScrub').dataset.settled==='true'")
    result=page.evaluate("""()=>{
      const at=Number(document.querySelector('#compareScrub').value),rows={};
      for(const name of ['Left','Right']){
        const video=document.getElementById('compare'+name),still=document.getElementById(video.id+'Still');
        const g=still.getContext('2d');let code=0;
        for(let b=0;b<8;b++)if(g.getImageData(b*10+4,4,1,1).data[0]>125)code+=2**b;
        rows[name]={pixel_id:code,clock:video.currentTime,paused:video.paused,visible:!!still.getClientRects().length,
          label:document.getElementById(video.id+'Pos').textContent};
      }
      return {at,...rows};
    }""")
    for name,rate in (("Left",24),("Right",60)):
        side=result[name]
        expected=min(3*rate-1,int((result["at"]+1e-12)*rate))
        assert side["pixel_id"]==expected and side["label"].startswith(f"Frame {expected} "),result
        assert side["visible"] and side["paused"] and abs(side["clock"]-result["at"])<.000003,result
    return result


@pytest.mark.skipif(os.name!="nt" or os.getenv("H3_NATIVE_UI_TESTS")!="1",
                    reason="Set H3_NATIVE_UI_TESTS=1 to launch an isolated Windows Studio window.")
def test_native_pause_matches_encoded_frames_and_both_labels(tmp_path):
    playwright=pytest.importorskip("playwright.sync_api")
    project,_,finish,*_=review_fixture(tmp_path)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1",0));port=listener.getsockname()[1]
    repo=Path(__file__).resolve().parents[1]
    local=repo/".venv"/"Scripts"/"pythonw.exe"
    python=local if local.is_file() else Path(sys.executable).with_name("pythonw.exe")
    env=dict(os.environ,WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=f"--remote-debugging-port={port} --disable-gpu --autoplay-policy=no-user-gesture-required")
    proc=subprocess.Popen([str(python),str(repo/"studio.py"),"--project",str(project.folder),"--home",str(tmp_path/"home")],env=env)
    try:
        with playwright.sync_playwright() as pw:
            deadline=time.monotonic()+25
            while True:
                try:
                    browser=pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}",timeout=1200)
                    break
                except playwright.Error:
                    if time.monotonic()>deadline or proc.poll() is not None:raise
                    time.sleep(.2)
            page=browser.contexts[0].pages[0]
            page.wait_for_function("window.H3?.get().doc")
            page.locator("[data-pane=shot]").click()
            page.locator(f'[data-take="{finish}"]').click()
            page.locator("#compareFinish").click()
            page.wait_for_function("document.querySelector('#compareRight')?.readyState>=2 && document.querySelector('#compareLeft')?.readyState>=2")
            for target in (.34,.624999,.625,.625001,.999998,1.000002):
                page.eval_on_selector("#compareScrub","(el,t)=>{el.value=t;el.dispatchEvent(new Event('input'));}",target)
                verify_native_hold(page)
            page.locator("#comparePlay").click()
            page.wait_for_function("document.querySelector('#compareLeft').currentTime>1.42")
            page.locator("#comparePlay").click()
            verify_native_hold(page)
    finally:
        if proc.poll() is None:proc.terminate()
        proc.wait(timeout=8)
