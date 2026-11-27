"""Portable project/desktop command integration for painting and source-frame edits."""
from pathlib import Path
import shutil
import time

from PIL import Image
import pytest

from h3studio.application import Studio
from h3studio.media import frame_index, render_storyboard
from h3studio.project import Project


@pytest.mark.skipif(not shutil.which("ffmpeg"),reason="FFmpeg required")
def test_source_brush_one_frame_and_persisted_range(tmp_path):
    project=Project.create(tmp_path/"project","Brush fixture")
    path=tmp_path/"red.png"
    Image.new("RGB",(80,64),"red").save(path)
    vid=tmp_path/"source.mp4"
    render_storyboard([dict(path=path,frame=0)],vid,frames=12,width=80,height=64)
    studio=Studio(tmp_path/"settings",project.folder)
    def op(command,**args):
        return studio.action(command,args)
    def done(t):
        limit=time.monotonic()+30
        while time.monotonic()<limit:
            result=next(j for j in op("jobs")["editorial"] if j["id"]==t["task"])
            if result["state"]=="completed":
                return result["result"]["take"]
            if result["state"] in ("failed","cancelled"):
                pytest.fail(result.get("error"))
            time.sleep(.05)
        pytest.fail("Editorial job timed out")
    try:
        clip=op("import_clip",path=str(vid))
        initial=project.state()["takes"][clip["take"]]
        take=project.save_take(clip["shot"],{**initial["settings"],"job_id":"accepted-generation"},
                               initial["video_asset"])

        # The frame displayed at 0.040 s is still the first 24 fps frame.
        info=op("edit_info",take=take,seconds=.040)
        assert info["source_index"]==0
        r=op("frame",take=take,seconds=.040)["revision"]
        stroke=dict(show=False,radius=.08,points=[[.2,.5],[.7,.5]])
        r2=op("paint_layer",revision=r,index=0,initial="full",strokes=[stroke])["revision"]
        a=project.state()["revisions"]
        assert "mask" not in a[r]["layers"][0]
        assert "mask" in a[r2]["layers"][0]
        edited=done(op("frame_branch",revision=r2))
        after=project.state()["takes"][edited]
        assert edited!=take and after["parent_take"]==take
        assert after["settings"]["source_job_id"]=="accepted-generation" and "job_id" not in after["settings"]
        assert project.asset_path(after["settings"]["preview_asset"]).is_file()
        assert after["settings"]["edit_manifest"]["corrections"][0]["index"]==0
        assert not after["settings"]["edited_ending"]
        # Keyframe and Hidden persist after reopening, including unchanged audio/video outside range.
        op("edit_anchor",take=take,source_index=5,state="keyframe",initial="clear",
           strokes=[dict(show=True,radius=.1,points=[[.3,.5]])])
        op("edit_anchor",take=take,source_index=6,state="hidden")
        key=op("edit_anchor",take=take,source_index=7,state="reappears",initial="clear",
           strokes=[dict(show=True,radius=.1,points=[[.4,.5]])])
        assert project.asset_path(key["mask"]).exists()
        next_take=done(op("edit_range",take=take,start_index=5,end_index=7,action="recolor",color="#3333ff"))
        reopened=Project(project.folder).state()
        f=reopened["takes"][next_take]
        assert f["parent_take"]==take
        manifest=f["settings"]["edit_manifest"]
        assert [k["state"] for k in manifest["corrections"]]==["keyframe","hidden","reappears"]
        assert all(project.asset_path(k["mask"]).is_file() for k in manifest["corrections"] if "mask" in k)
        assert len(frame_index(project.asset_path(f["video_asset"])))==12
        assert set((take,next_take,edited))<=set(reopened["shots"][clip["shot"]]["takes"])
    finally:
        studio.close()
