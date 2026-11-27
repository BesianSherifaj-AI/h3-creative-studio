"""Adjacent VFR source frames survive native review proxies and non-frame-aligned trims."""
from fractions import Fraction
from pathlib import Path
import shutil
import subprocess

from PIL import Image
import pytest

from h3studio import media
from h3studio.editor import editor_action
from h3studio.playback import source_window, take_preview
from h3studio.project import Project

pytestmark=pytest.mark.skipif(not shutil.which("ffmpeg"),reason="FFmpeg required")


def footage(path):
    pixels=b"".join(bytes(((n*47)%256,(n*119)%256,(n*71)%256))*(48*32) for n in range(18))
    subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-y","-f","rawvideo","-pix_fmt","rgb24",
                    "-s","48x32","-r","60000/1001","-i","pipe:0","-f","lavfi","-i","sine=f=1000:r=48000:d=0.304",
                    "-map","0:v","-map","1:a","-vf","select=not(eq(n\\,4)+eq(n\\,11))","-fps_mode","passthrough",
                    "-enc_time_base:v","1:60000","-video_track_timescale","60000","-c:v","libx264","-crf","12",
                    "-bf","0","-pix_fmt","yuv420p","-color_trc","bt709","-colorspace","bt709","-color_primaries","bt709",
                    "-c:a","aac",str(path)],input=pixels,check=True)


def pixel(path):
    with Image.open(path) as image:
        return image.convert("RGB").getpixel((24,16))


def test_cut_vfr_selection_exact_source_and_legacy_proxy(tmp_path):
    src=tmp_path/"vfr.mp4"
    footage(src)
    project=Project.create(tmp_path/"film","Source timing")
    asset=project.add_asset(src)
    shot=project.add_shot("VFR",media.probe_media(src)["duration"])
    # Legacy cuts and previous proxies can be reopened through the new timestamp path.
    take=project.save_take(shot,{"delivery_trim":[.019,.155],"preview_asset":asset},asset)
    preview=take_preview(project,take)
    frames,times,_,start,end,first,last=source_window(src,[.019,.155])
    pts=media.frame_index(preview)
    assert len(pts)==last-first+1 and abs(media.probe_media(preview)["duration"]-float(end-start))<.000002
    assert len(media.probe_media(preview)["audio_streams"])==1
    for off,entry in enumerate(pts):
        ix=first+off
        assert abs(entry["time_seconds"]-float(max(Fraction(0),times[ix]-start)))<.000002
        request={"take":take,"presented_seconds":entry["time_seconds"]}
        found=editor_action(None,project,"edit_info",request)
        assert found["source_index"]==ix and found["pts"]==frames[ix]["pts"]
        by_time=editor_action(None,project,"edit_info",{"take":take,"seconds":found["seek_seconds"]})
        assert by_time["source_index"]==ix
        opened=editor_action(None,project,"frame",request)
        rev=project.state()["revisions"][opened["revision"]]
        im=tmp_path/"ref.png";media.extract_frame(src,frames[ix]["pts"],im)
        assert pixel(project.asset_path(rev["layers"][0]["asset"]))==pixel(im)
    assert editor_action(None,project,"edit_info",{"take":take,"source_index":first,"step":1})["source_index"]==first+1
    # No 24 fps bins on the source route: two adjacent source frames occur within the first bin.
    assert pts[0]["time_seconds"] < pts[1]["time_seconds"] < 1/24
