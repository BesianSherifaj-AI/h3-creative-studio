"""Desktop source-frame, layer-mask and range-edit commands."""
from bisect import bisect_right
from fractions import Fraction
from pathlib import Path
import json
import math
import tempfile

from PIL import Image, ImageChops, ImageDraw, ImageOps
from . import media
from .playback import PROXY_VERSION, source_proxy, source_window


def _take(project, ident):
    state = project.state()
    take = state["takes"].get(ident)
    if not take or not take.get("video_asset"):
        raise ValueError("Select a rendered or imported take first.")
    path = project.asset_path(take["video_asset"])
    return state, take, path, *source_window(path,take["settings"].get("delivery_trim"))


def _selection(project, args):
    state, take, path, frames, times, meta, start, end, first, last = _take(project,args["take"])
    if "source_index" in args:
        index = args["source_index"]
        if type(index) is not int or not first <= index <= last:
            raise ValueError("Choose a source frame inside the take's delivered trim.")
    else:
        shown = "presented_seconds" in args
        seconds = media._ratio(args["presented_seconds"] if shown else args.get("seconds",0))
        # WebView presents media sample timestamps at microsecond precision. Roundoff at
        # a known frame boundary must not select the preceding source frame.
        tolerance = Fraction(2,1_000_000) if shown else 0
        index = min(last,max(first,bisect_right(times,start+seconds+tolerance)-1))
        if shown and "seconds" in args:
            # A previously queued compositor callback can arrive after a native seek.
            # Accept its PTS only while the paused clock still lies in that frame.
            clock = start+media._ratio(args["seconds"])
            stop = times[index+1] if index < last else end
            if clock < max(start,times[index])-tolerance or clock >= stop+tolerance:
                index = min(last,max(first,bisect_right(times,clock)-1))
    step=args.get("step",0)
    if type(step) is not int or step not in (-1,0,1):
        raise ValueError("Step by a single source frame.")
    index=min(last,max(first,index+step))
    entry = frames[index]
    begin=max(start,times[index])
    stop=min(end,times[index+1]) if index<last else end
    keys = state.get("edit_keys",{}).get(take["id"], {})
    info = dict(width=meta["width"], height=meta["height"], rotation=meta["rotation"],
                source_index=index, source_start=first, source_end=last, pts=entry["pts"],
                source_seconds=entry["time_seconds"], seconds=float(begin-start),
                seek_seconds=float((begin+stop)/2-start),
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
    return source_proxy(source,target,cancel=cancel)


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
                     "edit_manifest":manifest, "source_asset":take["video_asset"], "preview_asset":p.add_asset(preview),
                     "preview_timing":PROXY_VERSION}
            if origin_job := receipt.pop("job_id",None):
                receipt["source_job_id"] = origin_job
            receipt["edited_ending"] = bool(take["settings"].get("edited_ending") or receipt.get("edited_ending"))
            # The original motion remains an explicit alternative to the displayed edited ending.
            packet=take.get("packet_asset")
            ident=p.save_take(take["shot"],receipt,video,packet,take["id"],guides=take["guides"])
            return {"take":ident}
    return studio._background("Render source-frame edit",work)


def _play_masks(project,path,info,args,take):
    import base64
    import io
    from .media_edit import tracked_frames
    frames,times,meta,start,end,*_=source_window(path,take["settings"].get("delivery_trim"))
    at=info["source_index"]
    first,last=args["propagate_from"],args["play_end"]
    if not all(type(n) is int for n in (first,last)) or not info["source_start"]<=first<=at<=last<=info["source_end"]:
        raise ValueError("Play a range inside the delivered cut.")
    keys={k["index"]:{**k,"mask":project.asset_path(k["mask"]) if k.get("mask") else None}
          for k in info["keys"] if first<=k["index"]<=last}
    if first not in keys:
        raise ValueError("Set a correction on the first frame before playing the range.")
    nearest=max(i for i in keys if i<=at)
    resume=args.get("resume") or {}
    if (resume.get("index")==at-1 and nearest<at-1 and
            (resume.get("mask") or resume.get("state")=="hidden")):
        nearest=at-1
        keys[nearest]={"state":"tracked" if resume.get("mask") else "hidden",
                       "mask":project.asset_path(resume["mask"]) if resume.get("mask") else None}
    last=min(at+47,last)
    samples=[]
    continuation=None
    for ix,result in tracked_frames(path,meta,frames,{i:k for i,k in keys.items() if nearest<=i<=last},last):
        if ix<at and result["state"]!="ambiguous":
            continue
        mask=result.get("mask")
        label={"index":ix,"state":result["state"],"start":float(max(start,times[ix])-start),
               "end":float(min(end,times[ix+1])-start if ix+1<len(times) else end-start)}
        if mask is not None:
            full=Image.fromarray(mask)
            thumb=full.copy();thumb.thumbnail((480,320),Image.Resampling.LANCZOS)
            tint=Image.new("RGBA",thumb.size,(79,162,250))
            tint.putalpha(thumb.point(lambda p:round(p*.6)))
            data=io.BytesIO();tint.save(data,"PNG")
            label["overlay"]="data:image/png;base64,"+base64.b64encode(data.getvalue()).decode("ascii")
        samples.append(label)
        if ix==last and ix<args["play_end"] and result["state"]!="ambiguous":
            continuation={"index":ix,"state":result["state"],"mask":_store_mask(project,full) if mask is not None else None}
    return {"samples":samples,"resume":continuation}


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
            if "play_end" in a:
                return _play_masks(p,path,info,a,take)
            if "propagate_from" in a:
                from .media_edit import preview_tracking
                first=a["propagate_from"]
                at=info["source_index"]
                if type(first) is not int or not info["source_start"]<=first<=at:
                    raise ValueError("Inspect a frame at or after the start of the tracked range.")
                choices=[k for k in info["keys"] if first<=k["index"]<=at]
                if not choices or choices[0]["index"]!=first:
                    info["tracking"]={"state":"unmarked","detail":"Set a correction on the first frame of the range."}
                else:
                    anchor=choices[-1]
                    if anchor["state"]=="hidden" or anchor["index"]==at:
                        info["tracking"]={k:anchor[k] for k in ("state","mask") if k in anchor}
                    else:
                        frames,_,meta,*_=source_window(path)
                        tracking=preview_tracking(path,meta,frames,{**anchor,"mask":p.asset_path(anchor["mask"])},at)
                        if "mask" in tracking:
                            tracking["mask"]=_store_mask(p,Image.fromarray(tracking["mask"]))
                        info["tracking"]=tracking
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
            old=a.get("base_mask") or next((x.get("mask") for x in info["keys"] if x["pts"]==info["pts"]),None)
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
