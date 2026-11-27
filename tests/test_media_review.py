"""Native review uses the production tracker and each sample's presentation interval."""
import hashlib
import shutil
import subprocess
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
        frames.append(pixels.tobytes())
    a=np.zeros((64,96),dtype=np.uint8);a[16:26,8:18]=255
    Image.fromarray(a).save(mask)
    ff=["ffmpeg","-hide_banner","-loglevel","error","-y"]
    subprocess.run(ff+["-f","rawvideo","-pix_fmt","rgb24","-s","96x64","-r","24","-i","pipe:0",
                       "-f","lavfi","-i","sine=frequency=220:duration=3","-map","0:v","-map","1:a",
                       "-c:v","libx264","-pix_fmt","yuv420p","-crf","8","-c:a","aac",str(source)],
                   input=b"".join(frames),check=True)
    subprocess.run(ff+["-i",str(source),"-vf","fps=60","-c:v","libx264","-pix_fmt","yuv420p",
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
    tracked=inspect(8)["tracking"]
    assert inspect(7)["tracking"]["state"]=="tracked"
    assert tracked["state"]=="ambiguous" and tracked["at"]==8
    with Image.open(p.asset_path(tracked["mask"])) as mask:
        assert mask.getbbox()==(24,16,34,26)
    payload={"take":parent,"source_index":8,"state":"keyframe","initial":"current","base_mask":tracked["mask"],
             "strokes":[{"show":False,"radius":.025,"points":[[.26,.29]]}]}
    editor_action(None,p,"edit_anchor",payload)
    corrected=inspect(8)["tracking"]
    assert corrected["state"]=="keyframe" and corrected["mask"]!=tracked["mask"]
    assert inspect(9)["tracking"]["state"] in ("tracked","ambiguous")
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
    job["payload"]["studio_project"]="other"
    with pytest.raises(ValueError,match="owned"):finish_comparison(stub,p,{"job":job["id"]})
