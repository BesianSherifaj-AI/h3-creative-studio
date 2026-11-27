"""Desktop source-frame, layer-mask and range-edit commands."""
from bisect import bisect_left, bisect_right
from functools import lru_cache
from pathlib import Path
import json
import math
import tempfile

from PIL import Image, ImageChops, ImageDraw, ImageOps
from . import media


@lru_cache(maxsize=8)
def _timeline(path, size, mtime):
    frames = media.frame_index(path)
    if not frames:
        raise ValueError("This source has no decoded frames.")
    return frames, [f["time_seconds"] for f in frames], media.probe_media(path)


def _take(project, ident):
    state = project.state()
    take = state["takes"].get(ident)
    if not take or not take.get("video_asset"):
        raise ValueError("Select a rendered or imported take first.")
    path = project.asset_path(take["video_asset"])
    stat = path.stat()
    frames, times, meta = _timeline(str(path), stat.st_size, stat.st_mtime_ns)
    start, end = take["settings"].get("delivery_trim", [0., meta["duration"]])
    first = max(0, bisect_right(times, start + 1e-9)-1)
    last = min(len(frames)-1, max(first, bisect_left(times, end - 1e-9)-1))
    return state, take, path, frames, times, meta, start, end, first, last


def _selection(project, args):
    state, take, path, frames, times, meta, start, end, first, last = _take(project,args["take"])
    if "source_index" in args:
        index = args["source_index"]
        if type(index) is not int or not first <= index <= last:
            raise ValueError("Choose a source frame inside the take's delivered trim.")
    else:
        seconds = float(args.get("seconds", 0))
        if not math.isfinite(seconds):
            raise ValueError("Enter a finite playhead time.")
        index = min(last, max(first, bisect_right(times, start + seconds + 1e-9)-1))
    entry = frames[index]
    keys = state.get("edit_keys",{}).get(take["id"], {})
    info = dict(width=meta["width"], height=meta["height"], rotation=meta["rotation"],
                source_index=index, source_start=first, source_end=last, pts=entry["pts"],
                source_seconds=entry["time_seconds"], seconds=max(0., entry["time_seconds"]-start),
                time_base=meta["video_time_base"], keys=sorted(keys.values(),key=lambda k:k["index"]))
    return take, path, info


def _snapshot(project, source, info):
    with tempfile.TemporaryDirectory() as folder:
        out = Path(folder)/f"Frame {info['source_index']}.png"
        media.extract_frame(source,info["pts"],out)
        return project.add_asset(out)


def _paint_mask(dimensions, args, existing=None):
    initial = args.get("initial", "current")
    if initial not in ("current", "clear", "full"):
        raise ValueError("Choose a mask starting point.")
    if existing and initial == "current":
        with Image.open(existing) as handle:
            mask = ImageOps.exif_transpose(handle).convert("L")
        if mask.size != dimensions:
            raise ValueError("The saved mask dimensions differ from this frame.")
    else:
        mask = Image.new("L",dimensions,0 if initial == "clear" else 255)
    draw = ImageDraw.Draw(mask)
    for stroke in args.get("strokes", []):
        radius = float(stroke["radius"])
        if not math.isfinite(radius) or not 0 < radius <= 1:
            raise ValueError("Invalid normalized brush radius.")
        r = max(.5, radius * min(dimensions))
        points = []
        for pair in stroke["points"]:
            if len(pair) != 2 or not all(type(n) in (int,float) and math.isfinite(n) and 0 <= n <= 1 for n in pair):
                raise ValueError("Invalid brush point.")
            points.append((round(pair[0]*(dimensions[0]-1)),round(pair[1]*(dimensions[1]-1))))
        if type(stroke["show"]) is not bool:
            raise ValueError("Choose add or erase for this stroke.")
        shade = 255 if stroke["show"] else 0
        if len(points) >= 2:
            draw.line(points,fill=shade,width=max(1,round(r*2)),joint="curve")
        for x,y in points:
            draw.ellipse((round(x-r),round(y-r),round(x+r),round(y+r)),fill=shade)
    return mask


def _store_mask(p, mask):
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder)/"Selection mask.png"
        mask.save(path)
        return p.add_asset(path)


def _archive(p, result):
    folder=Path(result["manifest"]).parent
    doc=json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    video=p.add_asset(result["path"])
    doc["video"]=video
    add={}
    # Deduplicated mask images are referenced by content hash inside the saved receipt.
    for key in doc["corrections"]:
        for field in ("image","mask"):
            if field in key:
                path=key[field]
                if path not in add:
                    add[path]=p.add_asset(folder/path)
                key[field]=add[path]
    return video, doc


def playback_proxy(source,target,cancel=None):
    """Create an SDR H.264 review copy while retaining the source-resolution master."""
    from fractions import Fraction
    m=media.probe_media(source)
    scale=min(1., 1280/max(m["width"],m["height"]))
    w=max(2,2*round(m["width"]*scale/2))
    h=max(2,2*round(m["height"]*scale/2))
    try:
        rate=Fraction(m["fps"])
        if not 1 <= rate <= 60:
            rate=Fraction(30)
    except (ValueError,ZeroDivisionError):
        rate=Fraction(30)
    return media.export_film([dict(path=source)],target,width=w,height=h,fps=str(rate),
                             audio_tracks=min(8,len(m["audio_streams"])),cancel=cancel)


def _run_edit(studio,p,take,path,keys,first_pts,last_pts,settings,action,color):
    def work(cancel):
        with tempfile.TemporaryDirectory() as folder:
            result=media.render_edit_branch(path,Path(folder)/"branch",keys,start_pts=first_pts,
                         end_pts=last_pts,action=action,color=color,cancel=cancel)
            preview=Path(folder)/"Frame edit preview.mp4"
            playback_proxy(result["path"],preview,cancel)
            video, manifest=_archive(p,result)
            if cancel():
                raise InterruptedError("Edit cancelled.")
            receipt={**take["settings"], **settings, "parent_packet_asset":take.get("packet_asset"),
                     "edit_manifest":manifest, "source_asset":take["video_asset"], "preview_asset":p.add_asset(preview)}
            if origin_job := receipt.pop("job_id",None):
                receipt["source_job_id"] = origin_job
            receipt["edited_ending"] = bool(take["settings"].get("edited_ending") or receipt.get("edited_ending"))
            # The original motion remains an explicit alternative to the displayed edited ending.
            packet=take.get("packet_asset")
            ident=p.save_take(take["shot"],receipt,video,packet,take["id"],guides=take["guides"])
            return {"take":ident}
    return studio._background("Render source-frame edit",work)


def editor_action(studio,p,op,a):
    if op == "paint_layer":
        r=p.state()["revisions"][a["revision"]]
        layers=r["layers"]
        index=a["index"]
        if type(index) is not int or not 0 <= index < len(layers):
            raise ValueError("Select a layer to paint.")
        layer=layers[index]
        with Image.open(p.asset_path(layer["asset"])) as handle:
            dims=ImageOps.exif_transpose(handle).size
        old=p.asset_path(layer["mask"]) if layer.get("mask") else None
        layer["mask"]=_store_mask(p,_paint_mask(dims,a,old))
        return {"revision":p.save_composition(r["name"],r["width"],r["height"],layers,
                                                r["composition"],base_revision=r["id"])}
    if op in ("frame","edit_info","edit_anchor"):
        take,path,info=_selection(p,a)
        if op=="edit_info":
            if a.get("preview"):
                info["asset"]=_snapshot(p,path,info)
            return info
        if op=="frame":
            # Check the canvas contract before decoding/importing a large still.
            if max(info["width"],info["height"])>8192 or info["width"]*info["height"]>24_000_000:
                raise ValueError("The layer canvas supports up to 8192 pixels per side and 24 megapixels.")
            asset=_snapshot(p,path,info)
            rev=p.save_composition(f"Frame {info['source_index']} · {info['source_seconds']:.3f}s",
                info["width"],info["height"],[dict(asset=asset,name="Source frame")])
            with p.transaction() as d:
                link = dict(take=take["id"],source=take["video_asset"],pts=info["pts"],
                            index=info["source_index"],time_base=info["time_base"],base_revision=rev)
                d.setdefault("source_frames",{})[d["revisions"][rev]["composition"]]=link
            return {"revision":rev,"pts":info["pts"]}
        state=a["state"]
        k=str(info["pts"])
        record=dict(state=state,pts=info["pts"],index=info["source_index"])
        if state in ("keyframe","reappears"):
            old=next((x.get("mask") for x in info["keys"] if x["pts"]==info["pts"]),None)
            dims=(info["width"],info["height"])
            if a.get("path"):
                with Image.open(a["path"]) as handle:
                    mask=ImageOps.exif_transpose(handle).convert("L")
            else:
                mask=_paint_mask(dims,a,p.asset_path(old) if old else None)
            if mask.size!=dims or not mask.getbbox():
                raise ValueError("Use a nonempty mask matching the upright source frame.")
            record["mask"]=_store_mask(p,mask)
        elif state not in ("hidden","delete"):
            raise ValueError("Choose a correction, Hidden, Reappears or Delete.")
        with p.transaction() as d:
            keys=d.setdefault("edit_keys",{}).setdefault(take["id"],{})
            if state=="delete":
                keys.pop(k,None)
            else:
                keys[k]=record
        return record
    if op=="edit_range":
        doc,take,path,frames,_,_,_,_,first,last=_take(p,a["take"])
        begin,end=a["start_index"],a["end_index"]
        if type(begin) is not int or type(end) is not int or not first<=begin<=end<=last:
            raise ValueError("Choose an inclusive range inside the delivered take.")
        keys=sorted((v for v in doc.get("edit_keys",{}).get(take["id"],{}).values()
                     if begin<=v["index"]<=end),key=lambda v:v["index"])
        if not keys or keys[0]["index"]!=begin:
            raise ValueError("Paint a correction or mark Hidden on the first frame of the range.")
        resolved=[{**k,**({"mask":str(p.asset_path(k["mask"]))} if k.get("mask") else {})} for k in keys]
        settings=dict(kind="range edit",source_keys=keys,edited_ending=take["settings"].get("edited_ending",False) or end==last)
        return _run_edit(studio,p,take,path,resolved,frames[begin]["pts"],frames[end]["pts"],
                         settings,a.get("action","recolor"),a.get("color","#657dff"))
    if op=="frame_branch":
        doc=p.state()
        rev=doc["revisions"][a["revision"]]
        link=doc.get("source_frames",{}).get(rev["composition"])
        if not link:
            raise ValueError("Open a source frame from the selected take before saving a one-frame edit.")
        _,take,path,frames,_,_,_,_,_,last=_take(p,link["take"])
        with Image.open(studio._revision(p,rev["id"])) as changed, Image.open(studio._revision(p,link["base_revision"])) as before:
            def flatten(im):
                base=Image.new("RGBA",im.size,"black")
                base.alpha_composite(im.convert("RGBA"))
                return base.convert("RGB")
            rgb=flatten(changed)
            diff=ImageChops.difference(flatten(before),rgb)
            rr,gg,bb=diff.split()
            mask=ImageChops.lighter(ImageChops.lighter(rr,gg),bb).point(lambda x:255 if x else 0)
            if not mask.getbbox():
                raise ValueError("Change the source-frame composition before saving its edit.")
            with tempfile.TemporaryDirectory() as folder:
                at=Path(folder)/"Edited frame.png"
                rgb.save(at)
                image=p.add_asset(at)
        mask_id=_store_mask(p,mask)
        keys=[dict(pts=link["pts"],state="keyframe",mask=str(p.asset_path(mask_id)),image=str(p.asset_path(image)))]
        settings=dict(kind="frame edit",source=link,revision=rev["id"],
                      edited_ending=take["settings"].get("edited_ending",False) or frames[last]["pts"]==link["pts"])
        return _run_edit(studio,p,take,path,keys,link["pts"],link["pts"],settings,"recolor","#657dff")
    raise ValueError("Unknown editor command.")
