"""Real CPU codec, timeline, sync and lossless edit coverage. Fixtures contain no user media."""
from fractions import Fraction
from pathlib import Path
import shutil
import subprocess

import numpy as np
from PIL import Image
import pytest

from h3studio.media import (export_film, extract_frame, frame_index, prepare_h3_join, probe_media,
                            render_edit_branch, render_storyboard)

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                                reason="FFmpeg and FFprobe are required")


def ff(*args):
    return subprocess.run(["ffmpeg","-hide_banner","-v","error","-nostdin","-y",*map(str,args)],
                          check=True, capture_output=True).stdout


def picture(path):
    return np.asarray(Image.open(path).convert("RGB"))


def audio(path, channel):
    raw = ff("-i",path,"-map",f"0:a:{channel}","-ac","1","-ar","48000","-f","f32le","pipe:1")
    return np.frombuffer(raw, dtype="<f4")


def markers(path, rate="24"):
    # Flash plus a 10 ms click at 0.5 s. Audio 0 and 1 carry different signals.
    ff("-f","lavfi","-i",f"color=c=black:s=96x64:r={rate}:d=1",
       "-f","lavfi","-i","aevalsrc=if(between(t\\,0.5\\,0.51)\\,sin(2*PI*1000*t)\\,0):s=48000:d=1",
       "-f","lavfi","-i","sine=frequency=337:sample_rate=48000:duration=1",
       "-vf","drawbox=c=white:t=fill:enable='between(t,0.5,0.55)'",
       "-map","0:v","-map","1:a","-map","2:a","-c:v","libx264","-pix_fmt","yuv420p",
       "-c:a","aac","-color_primaries","bt709","-color_trc","bt709","-colorspace","bt709",path)


def test_storyboard_exact_guides_retime_and_cancellation(tmp_path):
    guides = []
    for at, color in ((0,"red"),(5,"lime"),(11,"blue")):
        path = tmp_path/f"{color}.png"
        Image.new("RGB",(64,64),color).save(path)
        guides.append(dict(frame=at, path=path))
    film = tmp_path/"boards.mp4"
    result = render_storyboard(guides,film,frames=12,width=64,height=64)
    idx = frame_index(film)
    assert result["kind"] == "storyboard" and len(idx) == 12
    for i, pos in enumerate((0,5,11)):
        frame = tmp_path/"read.png"
        extract_frame(film,idx[pos]["pts"],frame)
        values = picture(frame)[30,30]
        assert values[i] > 240 and values.sum()-values[i] < 22
    snapshot = film.read_bytes()
    with pytest.raises(InterruptedError):
        render_storyboard(guides,film,frames=12,width=64,height=64,cancel=lambda:True)
    assert film.read_bytes() == snapshot
    with pytest.raises(ValueError, match="unique"):
        render_storyboard(guides+[guides[0]], film, frames=12,width=64,height=64)


def test_three_cuts_audio_routes_sync_vfr_rotation_and_full_decode(tmp_path):
    slow, fast = tmp_path/"slow.mp4", tmp_path/"fast.mp4"
    markers(slow)
    markers(fast, "60000/1001")
    # Irregularly drop frames without resetting timestamps, then attach a real display matrix.
    raw, portrait = tmp_path/"irregular.mp4",tmp_path/"portrait.mp4"
    ff("-i",fast,"-vf","select='not(eq(mod(n,7),2))'","-fps_mode","vfr","-map","0:v","-map","0:a",
       "-c:v","libx264","-c:a","copy",raw)
    ff("-display_rotation","90","-i",raw,"-map","0","-c","copy",portrait)
    m = probe_media(portrait)
    idx = frame_index(portrait)
    assert (m["width"],m["height"]) == (64,96)
    assert len({b["pts"]-a["pts"] for a,b in zip(idx,idx[1:])}) > 1
    out = tmp_path/"delivery.mp4"
    clips = [dict(path=slow,start=.25,end=.875,audio=[0,1]),
             dict(path=portrait,start=.25,end=.875,audio=[0,None]),
             dict(path=fast,start=.25,end=.875,audio=[0,1])]
    receipt = export_film(clips,out,width=96,height=96,fps="24",audio_tracks=2)
    assert receipt["frames"] == 45 and len(frame_index(out)) == 45
    assert len(probe_media(out)["audio_streams"]) == 2
    # Silence on the alternate track of the middle cut, tone on either side.
    one, two = audio(out,0),audio(out,1)
    assert abs(two[36000:38000]).max() < .006
    assert abs(two[18000:23000]).max() > .05 and abs(two[74000:78000]).max() > .05
    decoded = ff("-i",out,"-map","0:v:0","-pix_fmt","gray","-f","rawvideo","pipe:1")
    intensity = np.frombuffer(decoded,dtype="u1").reshape(-1,96,96).mean(axis=(1,2))
    for first in (0,15,30):
        flash = first + next(i for i,value in enumerate(intensity[first:first+15]) if value>95)
        sample_start = first*2000
        click = sample_start + np.flatnonzero(abs(one[sample_start:sample_start+30000])>.15)[0]
        assert abs(flash/24 - click/48000) <= 1/24 + .002
    ff("-v","error","-xerror","-i",out,"-map","0","-f","null","-")


def test_multiple_delivery_rates_hdr_and_atomic_cancel(tmp_path):
    source = tmp_path/"source.mp4"
    markers(source)
    hdr, sdr = tmp_path/"hdr.mp4",tmp_path/"sdr.mp4"
    result=export_film([dict(path=source,start=0,end=.5)],hdr,width=96,height=64,
                       fps="60000/1001",color="bt2020-pq",audio_tracks=0)
    m = probe_media(hdr)
    assert result["frames"] == len(frame_index(hdr)) == 30
    assert m["color_transfer"] == "smpte2084" and "10" in m["pix_fmt"]
    # A lossless edit branch keeps the encoded HDR transfer in its NUT stream tags.
    first=frame_index(hdr)[0]["pts"]
    edit=render_edit_branch(hdr,tmp_path/"hdr-branch",[dict(pts=first,state="hidden")],start_pts=first,end_pts=first)
    hdr_master=probe_media(edit["path"])
    assert hdr_master["color_transfer"]=="smpte2084" and hdr_master["color_primaries"]=="bt2020"
    export_film([dict(path=hdr)],sdr,width=96,height=64)
    assert probe_media(sdr)["color_transfer"] == "bt709"
    old=sdr.read_bytes()
    with pytest.raises(InterruptedError):
        export_film([dict(path=hdr)],sdr,width=96,height=64,cancel=lambda:True)
    assert old==sdr.read_bytes()


def test_source_pts_tracking_disappearance_and_manual_corrections(tmp_path):
    import av
    # Moving object and near-identical distractor, hidden for frames 3-4, returns elsewhere.
    folder=tmp_path/"frames"
    folder.mkdir()
    rng=np.random.default_rng(31)
    leather = rng.integers(25,180,(12,12),dtype=np.uint8)
    maskpaths={}
    positions=[8,10,12,None,None,30,32,34]
    for i,x in enumerate(positions):
        pixels=np.full((64,80,3),33,dtype=np.uint8)
        pixels[20:32,46:58,:]=leather[:,:,None]
        if x is not None:
            pixels[20:32,x:x+12,:]=leather[:,:,None]
            mask=np.zeros((64,80),dtype=np.uint8)
            mask[20:32,x:x+12]=255
            p=tmp_path/f"mask-{i}.png"
            Image.fromarray(mask).save(p)
            maskpaths[i]=p
        Image.fromarray(pixels).save(folder/f"{i:02}.png")
    source=tmp_path/"input.mkv"
    ff("-framerate","24","-i",folder/"%02d.png","-f","lavfi","-i","sine=duration=1",
       "-map","0:v","-map","1:a","-shortest","-c:v","ffv1","-pix_fmt","gbrp","-c:a","pcm_s16le",source)
    idx=frame_index(source)
    keys=[dict(pts=idx[0]["pts"],mask=maskpaths[0]),dict(pts=idx[3]["pts"],state="hidden"),
          dict(pts=idx[5]["pts"],state="reappears",mask=maskpaths[5])]
    branch=render_edit_branch(source,tmp_path/"edit",keys,start_pts=idx[0]["pts"],
                              end_pts=idx[-1]["pts"],color="#4949ff")
    assert [r["state"] for r in branch["corrections"]]==["keyframe","tracked","tracked","hidden","hidden","reappears","tracked","tracked"]
    def decoded(path):
        with av.open(str(path)) as c:
            return [(f.pts*f.time_base, f.to_ndarray(format="rgb48le")) for f in c.decode(video=0)]
    original, edited=decoded(source),decoded(branch["path"])
    assert len(original)==len(edited)==8
    for i,(a,b) in enumerate(zip(original,edited)):
        assert a[0]==b[0]
        if positions[i] is None:
            assert np.array_equal(a[1], b[1])
        else:
            mask=np.asarray(Image.open(maskpaths[i]))>0
            assert np.array_equal(a[1][~mask], b[1][~mask])
            assert b[1][mask][:,2].mean()>b[1][mask][:,0].mean()*1.8
            assert b[1][mask][:,0].std()>1000 # texture survives the tint
    assert np.array_equal(audio(source,0), audio(branch["path"],0))
    assert Path(branch["manifest"]).exists()


def test_full_resolution_wide_source_frame_and_invalid_pts(tmp_path):
    path=tmp_path/"wide.mkv"
    ff("-f","lavfi","-i","color=blue:s=4224x128:r=2:d=1","-c:v","ffv1",path)
    at=frame_index(path)[1]
    frame=tmp_path/"original.png"
    info=extract_frame(path,at["pts"],frame)
    assert info["width"]==4224 and Image.open(frame).size==(4224,128)
    with pytest.raises(ValueError,match="No source frame"):
        extract_frame(path,at["pts"]-1,tmp_path/"missing.png")

def test_rotated_single_frame_branch_keeps_timing_and_audio(tmp_path):
    original, rotated = tmp_path/"asymmetric.mp4",tmp_path/"turn.mp4"
    ff("-f","lavfi","-i","testsrc2=size=96x48:rate=24:duration=.5".replace("=.5","=0.5"),
       "-f","lavfi","-i","sine=duration=0.5","-map","0:v","-map","1:a",
       "-c:v","libx264","-pix_fmt","yuv420p","-c:a","aac",original)
    ff("-display_rotation","90","-i",original,"-c","copy",rotated)
    idx=frame_index(rotated)
    pts=idx[4]["pts"]
    marker=tmp_path/"correction.png"
    Image.new("RGB",(48,96),"pink").save(marker)
    b=render_edit_branch(rotated,tmp_path/"one", [dict(pts=pts,image=marker)],start_pts=pts,end_pts=pts)
    out=Path(b["path"])
    # Extract an unedited asymmetric frame and compare its orientation to the source.
    src,branch = tmp_path/"src.png",tmp_path/"out.png"
    extract_frame(rotated,idx[0]["pts"],src)
    extract_frame(out,frame_index(out)[0]["pts"],branch)
    assert np.abs(picture(src).astype(int)-picture(branch).astype(int)).mean()<2
    assert abs(probe_media(out)["duration"]-.5)<.001
    e=tmp_path/"share.mp4"
    export_film([dict(path=out)],e,width=48,height=96)
    assert len(frame_index(e))==len(idx)==12


def test_pale_shadow_removal_mask_preserves_the_surrounding_surface(tmp_path):
    from h3studio.media_edit import _paint
    # A pale cast shadow extends beyond the object. The correction mask covers both.
    base=np.full((96,128,3),52000,np.uint16)
    src=base.copy()
    src[42:54,52:73]=47400
    src[32:50,44:58]=12500
    mask=np.zeros((96,128),np.uint8)
    mask[41:55,51:74]=255
    mask[31:51,43:59]=255
    repaired=_paint(src,mask,"remove",(0,0,0))
    assert np.array_equal(repaired[mask==0],base[mask==0])
    assert np.abs(repaired[42:54,59:73].astype(float)-base[42:54,59:73]).mean()<8
