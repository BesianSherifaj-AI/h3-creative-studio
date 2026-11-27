"""Non-destructive edits over immutable takes and source-relative half-open trims."""
import base64
import copy
from fractions import Fraction
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import uuid

from . import media


RATE=24


def _snapshot(doc):
    return [{"shot":s,"take":doc["shots"][s]["selected_take"]} for s in doc["cut"]]


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":")).encode()).hexdigest()


def _version(doc):
    return _digest([doc["id"],_snapshot(doc),[doc["shots"][s].get("locked_take") for s in doc["cut"]],
                    doc.get("soundtracks",{})])


@lru_cache(maxsize=128)
def _probe(path,size,mtime):
    return media.probe_media(path)


def _metadata(path):
    stat=path.stat()
    return _probe(str(path),stat.st_size,stat.st_mtime_ns)


def _bounds(take,meta):
    end=Fraction(meta["duration_exact"])
    raw=take["settings"].get("delivery_trim")
    if not raw:return Fraction(0),end
    a,b=(media._ratio(v) for v in raw)
    if abs(b-end)<Fraction(1,1_000_000):b=end
    if not 0<=a<b<=end:raise ValueError("The take trim exceeds its source duration.")
    return a,b


def _view(project,doc):
    clips=[]
    audio_tracks=max([1]+[b["track"]+1 for b in doc.get("soundtracks",{}).values()])
    total=Fraction(0)
    for shot_id in doc["cut"]:
        shot=doc["shots"][shot_id]
        take=doc["takes"].get(shot["selected_take"])
        item=dict(shot=shot_id,take=shot["selected_take"],name=shot["name"],
                  locked=bool(shot.get("locked_take")),start_frame=round(total*RATE),source_offset=str(total),asset=None,
                  source_in=0,source_out=shot["duration"],source_duration=shot["duration"],audio=None)
        duration=media._ratio(shot["duration"])
        if take and take.get("video_asset"):
            try:
                path=project._asset_path(doc,take["video_asset"]);meta=_metadata(path)
                start,end=_bounds(take,meta)
                duration=end-start
                route=take["settings"].get("audio_route") or [0 if meta["audio_streams"] else None]
                audio_tracks=max(audio_tracks,len(take["settings"].get("audio_route") or meta["audio_streams"]))
                item.update(asset=take["video_asset"],source_in=float(start),source_out=float(end),
                            source_duration=meta["duration"],audio=route[0],fps=meta["fps"],
                            width=meta["width"],height=meta["height"],kind=take["settings"].get("kind","H3"))
            except (OSError,ValueError) as exc:
                item["error"]=str(exc)
        total+=duration
        item["end_frame"]=round(total*RATE)
        clips.append(item)
    history=doc.get("timeline_edits",[])
    cursor=doc.get("timeline_cursor",len(history))
    current=_snapshot(doc)
    previous=history[cursor-1] if cursor and history[cursor-1]["after"]==current else None
    following=history[cursor] if cursor<len(history) and history[cursor]["before"]==current else None
    return dict(project=doc["id"],name=doc["name"],version=_version(doc),fps=RATE,frames=round(total*RATE),
                clips=clips,beds=list(doc.get("soundtracks",{}).values()),audio_tracks=min(audio_tracks,8),
                undo=previous["label"] if previous else None,redo=following["label"] if following else None)


def _remember(doc,before,label):
    history=doc.setdefault("timeline_edits",[])
    del history[doc.get("timeline_cursor",len(history)):]
    history.append(dict(before=before,after=_snapshot(doc),label=label))
    doc["timeline_cursor"]=len(history)


def _trimmed_take(project,doc,shot,take,start,end):
    if end-start<Fraction(1,24):
        raise ValueError("Keep at least one delivery frame on each side of the edit.")
    settings=copy.deepcopy(take["settings"])
    settings["delivery_trim"]=[float(start),float(end)]
    settings["edited_ending"]=bool(settings.get("edited_ending") or end!=_bounds(take,_metadata(project._asset_path(doc,take["video_asset"])))[1])
    if original:=settings.pop("job_id",None):settings["source_job_id"]=original
    ident=uuid.uuid4().hex
    branch={**copy.deepcopy(take),"id":ident,"parent_take":take["id"],"shot":shot["id"],
            "settings":settings,"trim":project._take_trim(settings),"duration":shot["duration"]}
    doc["takes"][ident]=branch
    shot["takes"].append(ident)
    shot["selected_take"]=ident
    return ident


def _mutate(project,op,args):
    with project.transaction() as doc:
        if args.get("version")!=_version(doc):raise ValueError("The timeline changed. Refresh before editing it.")
        before=_snapshot(doc)
        project._remember_cut(doc)
        selected=args.get("shot")
        if op in ("cut_undo","cut_redo"):
            history=doc.get("timeline_edits",[])
            cursor=doc.get("timeline_cursor",len(history))
            undo=op=="cut_undo"
            pos=cursor-1 if undo else cursor
            if not 0<=pos<len(history):raise ValueError("There is no edit to restore.")
            entry=history[pos]
            if before!=entry["after" if undo else "before"]:raise ValueError("The cut changed outside the timeline editor.")
            target=entry["before" if undo else "after"]
            selected_takes={r["shot"]:r["take"] for r in target}
            for shot_id in set(doc["cut"])|set(selected_takes):
                locked=doc["shots"][shot_id].get("locked_take")
                if locked and selected_takes.get(shot_id)!=locked:raise ValueError("Unlock this Film take before changing its cut.")
            if target:project._apply_cut(doc,target)
            else:doc["cut"]=[]
            doc["timeline_cursor"]=cursor+(-1 if undo else 1)
            return {}
        if selected not in doc["cut"]:raise ValueError("Choose a shot in the current timeline.")
        shot=doc["shots"][selected]
        if op=="cut_reorder":
            order=args["order"]
            if not isinstance(order,list) or sorted(order)!=sorted(doc["cut"]):raise ValueError("Choose an ordering of the current shots.")
            if order==doc["cut"]:return {"shot":selected}
            doc["cut"]=order
            label="Move shot"
        else:
            if shot.get("locked_take"):raise ValueError("Unlock this Film take before editing it.")
            if op=="cut_remove":
                index=doc["cut"].index(selected);doc["cut"].remove(selected)
                selected=doc["cut"][min(index,len(doc["cut"])-1)] if doc["cut"] else None
                label="Remove shot"
            else:
                take=doc["takes"].get(shot["selected_take"])
                if not take or not take.get("video_asset"):raise ValueError("Select a rendered take before trimming.")
                meta=_metadata(project._asset_path(doc,take["video_asset"]))
                a,b=_bounds(take,meta)
                if op=="cut_trim":
                    start,end=media._ratio(args["start"]),media._ratio(args["end"])
                    ceiling=Fraction(meta["duration_exact"])
                    if abs(end-ceiling)<Fraction(1,1_000_000):end=ceiling
                    if not 0<=start<end<=ceiling:raise ValueError("Trim inside the source recording.")
                    if (start,end)==(a,b):return {"shot":selected}
                    _trimmed_take(project,doc,shot,take,start,end)
                    label="Trim shot"
                elif op=="cut_split":
                    frame=args["frame"]
                    view=_view(project,doc)
                    row=next(r for r in view["clips"] if r["shot"]==selected)
                    if type(frame)!=int or not row["start_frame"]<frame<row["end_frame"]:
                        raise ValueError("Place the playhead inside the selected shot.")
                    # Translate the delivery-frame boundary through cumulative source time, including earlier fractional cuts.
                    offset=Fraction(row["source_offset"])
                    split=a+Fraction(frame,RATE)-offset
                    _trimmed_take(project,doc,shot,take,a,split)
                    tail=project._shot(shot["name"]+" · tail",float(b-split))
                    _trimmed_take(project,doc,tail,take,split,b)
                    doc["shots"][tail["id"]]=tail
                    doc["cut"].insert(doc["cut"].index(selected)+1,tail["id"])
                    selected=tail["id"];label="Split shot"
                else:raise ValueError("Unknown timeline edit.")
        _remember(doc,before,label)
    return {"shot":selected}


def _read(args):
    result=subprocess.run(args,capture_output=True,creationflags=media._HIDDEN)
    if result.returncode:raise ValueError(result.stderr.decode("utf-8","replace")[-1400:] or "Media inspection failed.")
    return result.stdout


def _peaks(path,stream,start,duration,origin=0,gain=0):
    bins=160
    if stream is None:return [0]*bins
    samples=round(duration*48000)
    if samples<1:return [0]*bins
    stride=min(48000,max(1,math.ceil(samples/bins)))
    absolute=float(start+origin)
    filters=(f"atrim=start={absolute:.9f}:end={absolute+duration:.9f},asetpts=PTS-({absolute:.9f})/TB,"
             f"aresample=48000:async=1:first_pts=0,volume={gain}dB,apad,atrim=end_sample={samples},"
             f"asetnsamples=n={stride}:p=1,astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=Peak_level,"
             "ametadata=print:key=lavfi.astats.Overall.Peak_level:file=-")
    args=[*media._ffmpeg(),"-copyts","-protocol_whitelist","file,pipe","-format_whitelist",media.AUDIO_FORMATS,
          "-ss",f"{float(start):.9f}","-i",str(path),"-vn","-map",f"0:a:{stream}","-af",filters,"-f","null",os.devnull]
    peaks=[0]*bins
    levels=[float(line.partition("=")[2]) for line in _read(args).decode().splitlines() if line.startswith("lavfi.astats.Overall.Peak_level=")]
    for n,db in enumerate(levels):
        dest=min(bins-1,n*stride*bins//samples)
        peaks[dest]=max(peaks[dest],round(min(1,10**(db/20)),4))
    return peaks


def _thumbnail(path,time):
    data=_read([*media._ffmpeg(),*media._INPUT,"-ss",f"{time:.8f}","-i",str(path),"-map","0:V:0","-an",
                "-frames:v","1","-vf","scale=128:72:force_original_aspect_ratio=decrease,pad=128:72:(ow-iw)/2:(oh-ih)/2:black",
                "-c:v","mjpeg","-q:v","5","-f","image2pipe","pipe:1"])
    return "data:image/jpeg;base64,"+base64.b64encode(data).decode("ascii") if data else None


def _route(take,meta,track):
    explicit=take["settings"].get("audio_route")
    if explicit:return explicit[track] if track<len(explicit) else None
    return track if track<len(meta["audio_streams"]) else None


def _strip(project,doc,args,track):
    if args.get("bed"):
        bed=doc["soundtracks"][args["bed"]]
        path=project._asset_path(doc,bed["asset"])
        raw=_read([media._bin("ffprobe"),"-v","error","-protocol_whitelist","file,pipe","-format_whitelist",media.AUDIO_FORMATS,
                   "-show_entries","format=duration,start_time","-of","json",str(path)])
        info=json.loads(raw)["format"]
        duration=float(info["duration"]);start=0;origin=float(info.get("start_time") or 0)
        stream=bed["stream"];gain=bed["gain_db"];key=["bed",bed["asset"],stream,gain];video=False
    else:
        take=doc["takes"][args["take"]]
        path=project._asset_path(doc,take["video_asset"]);meta=_metadata(path);a,b=_bounds(take,meta)
        start=float(a);duration=float(b-a);origin=meta["start_pts"]*Fraction(meta["video_time_base"])
        stream=_route(take,meta,track);gain=0;key=["video",take["video_asset"],str(a),str(b),stream];video=True
    cache=project.folder/"preview-cache"/("strip-"+_digest(key)+".json")
    if cache.is_file():return json.loads(cache.read_text(encoding="utf-8"))
    images=[_thumbnail(path,start+duration*f) for f in (.05,.45,.82)] if video else []
    result={"images":[x for x in images if x],"peaks":_peaks(path,stream,start,duration,origin,gain),
            "duration":duration,"stream":stream}
    cache.parent.mkdir(exist_ok=True)
    work=cache.with_suffix("."+uuid.uuid4().hex+".tmp")
    try:
        work.write_text(json.dumps(result,separators=(",",":")),encoding="utf-8");os.replace(work,cache)
    finally:work.unlink(missing_ok=True)
    return result


def _preview(studio,project,doc,track):
    token=_digest(["review-v1",_version(doc),track])
    cached=doc.get("cut_previews",{}).get(token)
    if cached and cached in doc["assets"]:
        project._asset_path(doc,cached)
        return {"asset":cached}
    pending=getattr(studio,"cut_preview_tasks",{})
    if (tid:=pending.get(token)) and studio.tasks[tid]["state"] in ("queued","running"):
        return {"task":tid}
    view=_view(project,doc)
    if not view["clips"]:raise ValueError("Add a shot before playing the film.")
    clips=[]
    for row in view["clips"]:
        if not row["asset"]:raise ValueError("Render or relink the take for "+row["name"]+" before playing the film.")
        t=doc["takes"][row["take"]];path=project._asset_path(doc,row["asset"]);meta=_metadata(path)
        start,end=_bounds(t,meta)
        clips.append(dict(path=path,start=start,end=end,audio=[_route(t,meta,track)]))
    beds=[{**b,"track":0,"path":project._asset_path(doc,b["asset"])} for b in view["beds"] if b["track"]==track]
    first=view["clips"][0]
    scale=min(1,960/max(first["width"],first["height"]))
    size=[max(2,round(first[k]*scale/2)*2) for k in ("width","height")]
    def build(cancel):
        with tempfile.TemporaryDirectory() as tmp:
            review=Path(tmp)/"Film preview.mp4"
            receipt=media.export_film(clips,review,width=size[0],height=size[1],fps=str(RATE),audio_tracks=1,soundtracks=beds,cancel=cancel)
            if cancel():raise InterruptedError("Film preview cancelled.")
            asset=project.add_asset(review)
            with project.transaction() as state:state.setdefault("cut_previews",{})[token]=asset
            return {"asset":asset,"frames":receipt["frames"]}
    task=studio._background("Prepare film playback",build)
    pending[token]=task["task"];studio.cut_preview_tasks=pending
    return task


def cut_action(studio,project,op,args):
    if op in ("cut_trim","cut_split","cut_reorder","cut_remove","cut_undo","cut_redo"):
        return _mutate(project,op,args)
    doc=project.state()
    if op=="cut_view":return _view(project,doc)
    track=args.get("track",0)
    if type(track)!=int or not 0<=track<8:raise ValueError("Choose a valid film audio track.")
    if args.get("version")!=_version(doc):raise ValueError("The timeline changed. Refresh the edit room.")
    if op=="cut_strip":return _strip(project,doc,args,track)
    if op=="cut_preview":return _preview(studio,project,doc,track)
    raise ValueError("Unknown timeline action.")
