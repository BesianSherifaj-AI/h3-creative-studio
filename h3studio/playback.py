"""Source-cadence review media and the timestamp map shared with frame editing."""
from bisect import bisect_left, bisect_right
from fractions import Fraction
from functools import lru_cache
from pathlib import Path

from . import media


PROXY_VERSION = "source-pts-1"
CLOCK = 1_000_000


@lru_cache(maxsize=8)
def _timeline(path, size, mtime):
    meta = media.probe_media(path)
    frames = media.frame_index(path)
    if not frames:
        raise ValueError("This source has no decoded frames.")
    tick = Fraction(meta["video_time_base"])
    times = [(item["pts"]-meta["start_pts"])*tick for item in frames]
    return frames, times, meta


def source_window(source, trim=None):
    path = Path(source)
    stat = path.stat()
    frames, times, meta = _timeline(str(path), stat.st_size, stat.st_mtime_ns)
    duration = Fraction(meta["duration_exact"])
    start, end = (Fraction(str(t)) for t in trim) if trim else (Fraction(0), duration)
    # Some legacy manifests serialized a rational stream duration as a double.
    if abs(end-duration) <= Fraction(1,1_000_000):
        end = duration
    if not 0 <= start < end <= duration:
        raise ValueError("The review trim falls outside the source.")
    first = max(0,bisect_right(times,start)-1)
    last = max(first,bisect_left(times,end)-1)
    return frames, times, meta, start, end, first, last


def source_proxy(source, target, *, trim=None, routing=None, cancel=None):
    """Encode one preview sample per visible source frame, including a partially visible first/last."""
    frames,times,meta,start,end,first,last = source_window(source,trim)
    factor = min(1,1280/max(meta["width"],meta["height"]))
    width,height = [max(2,2*round(meta[k]*factor/2)) for k in ("width","height")]
    origin = meta["start_pts"]*Fraction(meta["video_time_base"])
    first_tick,end_tick = frames[first]["pts"],frames[last]["pts"]+1
    offset = round((start+origin)*CLOCK)
    length = round((end-start)*CLOCK)
    video = (f"[0:{meta['video_stream']}]trim=start_pts={first_tick}:end_pts={end_tick},"
             f"settb=1/{CLOCK},setpts=max(0\\,PTS-({offset})),{media._delivery_color(meta,'bt709')},"
             f"scale={width}:{height}:reset_sar=1,setsar=1[v]")
    filters = [video]
    if routing is None:
        routing = list(range(min(8,len(meta["audio_streams"]))))
    for i, selected in enumerate(routing):
        if selected is None:
            audio = "anullsrc=r=48000:cl=stereo,"
        else:
            if type(selected) is not int or not 0 <= selected < len(meta["audio_streams"]):
                raise ValueError("Invalid source audio track for playback.")
            index=meta["audio_streams"][selected]["index"]
            audio=(f"[0:{index}]atrim=start={media._seconds(start+origin)}:end={media._seconds(end+origin)},"
                   f"asetpts=PTS-({media._seconds(start+origin)})/TB,aresample=48000:async=1:first_pts=0,apad,")
        filters.append(audio+f"atrim=end_sample={round((end-start)*48000)},asetpts=N/SR/TB[a{i}]")
    with media._output(target,[source]) as temp:
        args = [*media._ffmpeg(),"-copyts",*media._INPUT,"-i",str(source),"-filter_complex",";".join(filters),"-map","[v]"]
        for i in range(len(routing)):
            args += ["-map",f"[a{i}]"]
        # No B-frame reordering. The last MP4 sample ends at the selected cut even when
        # the source sample spans past it or the VFR stream has a long final frame.
        tail=f"setts=duration=if(eq(N\\,{last-first})\\,{length}-PTS\\,DURATION)"
        args += [*media._encode("bt709"),"-bf","0","-fps_mode","passthrough","-enc_time_base:v",f"1:{CLOCK}",
                 "-video_track_timescale",str(CLOCK),"-bsf:v",tail,"-c:a","aac","-b:a","192k",
                 "-movflags","+faststart","-f","mp4",str(temp)]
        media._run(args,cancel)
    return {"path":str(target),"frames":last-first+1,"duration":float(end-start),"timing":PROXY_VERSION}


def take_preview(project, take_id):
    take = project.state()["takes"][take_id]
    options = take["settings"]
    source = project.asset_path(take["video_asset"])
    delivery = options.get("delivery_trim")
    if not delivery:
        if options.get("preview_asset") and options.get("preview_timing")==PROXY_VERSION:
            return project.asset_path(options["preview_asset"])
        if not options.get("preview_asset") and _direct_playback(source):
            return source
    # Old CFR review copies remain archived. Derive a new source-timed copy from the master.
    cache = project.folder/"preview-cache"
    output = cache/(take_id+"-"+PROXY_VERSION+".mp4")
    if not output.is_file():
        routing = options.get("audio_route")
        source_proxy(source,output,trim=delivery,routing=routing[:1] if routing else None)
    return output


def _direct_playback(path):
    meta=media.probe_media(path)
    if meta["start_pts"]!=0:
        return False
    suffix=path.suffix.lower()
    return ((suffix in (".mp4",".m4v",".mov") and meta["video_codec"]=="h264" and
             meta["pix_fmt"] in ("yuv420p","yuvj420p")) or
            (suffix==".webm" and meta["video_codec"] in ("vp8","vp9","av1")))


def _review_clock(path,trim):
    """Run-length encode source sample intervals; a constant-cadence hour needs one run."""
    _,times,meta,start,end,first,last=source_window(path,trim)
    runs=[]
    for ix in range(first,last+1):
        position=max(start,times[ix])
        next_time=min(end,times[ix+1]) if ix+1<len(times) else end
        interval=next_time-position
        if runs and runs[-1][2]==interval:
            runs[-1][3]+=1
        else:
            runs.append([ix,position-start,interval,1])
    return {"runs":[[ix,float(t),float(delta),count] for ix,t,delta,count in runs],
            "duration":float(end-start),"fps":meta["fps"]}


def finish_comparison(studio,project,args):
    doc=project.state()
    if args.get("job"):
        job=studio.queue.get(args["job"],full=True)
        pl=job["payload"]
        if job["kind"]!="finish" or job["status"]!="succeeded" or pl.get("studio_project")!=doc["id"]:
            raise ValueError("Choose a completed finishing job owned by this project.")
        if not pl.get("parent_take") or pl["parent_take"] not in doc["takes"]:
            raise ValueError("This finishing job has no source take in this project.")
        result=job["receipt"]["result"]
        from .integrations import _archive_result
        digest=result.get("sha256")
        asset=None
        if digest in doc["assets"]:
            try:
                project.asset_path(digest)
                asset=digest
            except ValueError:
                pass
        if not asset:
            asset=_archive_result(project,result["output_path"],digest)
        parent=doc["takes"][pl["parent_take"]]
        trim=pl.get("delivery_trim")
        route=pl.get("audio_route")
        key=job["id"]
    else:
        finished=doc["takes"][args["take"]]
        if finished["settings"].get("kind")!="finish" or not finished.get("parent_take"):
            raise ValueError("Choose a finished take with an archived source.")
        original=finished
        parent=doc["takes"][original["parent_take"]]
        while (not original["settings"].get("job_id") and parent["settings"].get("kind")=="finish" and
               parent["video_asset"]==finished["video_asset"] and parent.get("parent_take")):
            original=parent
            parent=doc["takes"][original["parent_take"]]
        asset=finished["video_asset"]
        trim=finished["settings"].get("delivery_trim")
        route=finished["settings"].get("audio_route")
        key=finished["settings"].get("job_id") or finished["id"]
    source=project.asset_path(parent["video_asset"])
    output=project.asset_path(asset)
    if trim:
        # Interpolation can round the last frame to the output time base.
        right_trim=[trim[0],min(Fraction(str(trim[1])),Fraction(media.probe_media(output)["duration_exact"]))]
    else:
        right_trim=None
    right_clock=_review_clock(output,right_trim)
    left_clock=_review_clock(source,trim)
    def view(path,clip,suffix,asset_id):
        if not clip and (not route or route[0]==0) and _direct_playback(path):
            return "/asset/"+asset_id
        cache=project.folder/"preview-cache"/(key+"-compare-"+suffix+"-"+PROXY_VERSION+".mp4")
        if not cache.is_file():
            source_proxy(path,cache,trim=clip,routing=route[:1] if route else None)
        return "/asset/"+project.add_asset(cache)
    return {"source":{**left_clock,"url":view(source,trim,"source",parent["video_asset"])},
            "finished":{**right_clock,"url":view(output,right_trim,"finish",asset)},
            "duration":min(left_clock["duration"],right_clock["duration"]),"parent":parent["id"]}
