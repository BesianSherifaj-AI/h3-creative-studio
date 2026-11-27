"""Adapt project revisions and approved plans to explicit local render workers."""
from __future__ import annotations

import copy
import hashlib
import tempfile
import uuid
from pathlib import Path

import httpx

from vendor.prompt import comfy_transfer
from vendor.prompt.resources import local_url
from . import generation, media
from .jobs import game_guard


def _kind(guide):
    role = guide.get("role", "image")
    if role in ("first", "middle", "last", "first_frame", "last_frame", "reference_image"):
        return "image"
    return role.removeprefix("reference_")


def _ending_guide(p, take, shot, target):
    if shot.get("start_revision"):
        return {"revision": shot["start_revision"], "frame": 0, "role": "image"}
    if not take or not take.get("video_asset"):
        raise ValueError("The parent has no displayed video. Select an edited ending revision.")
    src = p.asset_path(take["video_asset"])
    index = media.frame_index(src)
    if not index:
        raise ValueError("The parent video has no displayable frames.")
    stop = take["settings"].get("delivery_trim", [0, None])[1]
    frame = next((f for f in reversed(index) if stop is None or f["time_seconds"] < stop - 1e-8), index[0])
    media.extract_frame(src, frame["pts"], target)
    with target.open("rb") as stream:
        digest = hashlib.file_digest(stream,"sha256").hexdigest()
    return {"asset": digest, "frame": 0, "role": "image"}


def _visual_guides(guides, first, mode):
    """Override only the timed visual endpoint for this render."""
    effective = [first]
    for guide in guides:
        if guide["frame"] != 0 or _kind(guide) != "image":
            effective.append(guide)
        elif mode == "ref2va" and guide.get("usage","both") != "timed":
            effective.append({**guide, "usage":"reference"})
        elif guide.get("audio_asset"):
            effective.append({"frame":0,"asset":guide["audio_asset"],"role":"audio","usage":"timed"})
    return effective


def _parent_spec(p, doc, take, start, paths):
    if not take or not take.get("packet") or not take.get("packet_asset"):
        raise ValueError("A verified saved motion packet is required.")
    m, packet = take["settings"], take["packet"]
    asset = doc["assets"][take["packet_asset"]]
    paths[asset["id"]] = p.asset_path(asset["id"])
    trim = m.get("render_trim", [0, round(take["duration"] * 24)])
    if m.get("delivery_trim"):
        trim = [round(s * 24) for s in m["delivery_trim"]]
    return {"take_id": take["id"], "packet_id": packet["id"], "packet_revision": m.get("motion_revision") or packet["revision"],
            "packet_asset_id": asset["id"], "sha256": asset["sha256"], "latent_revision": packet["revision"],
            "selector": m.get("motion_selector") or m.get("selector") or "output::restored/" + asset["sha256"] + ".mmh3",
            "trim": trim, "render_frames": packet["frames"], "ending_edited": bool(m.get("edited_ending")),
            "use_original_motion": start == "original_motion", "overlap": 39}


def _video(studio, p, a, doc, shot):
    with tempfile.TemporaryDirectory() as folder:
        return _video_graph(studio,p,a,doc,shot,Path(folder)/"Displayed ending.png")


def _video_graph(studio, p, a, doc, shot, ending_file):
    mode = a.get("mode", "t2va")
    timeline = generation.timing(shot["duration"])
    paths, assets, timed, refs = {}, [], [], {}
    if mode in ("i2va", "fl2va"):
        refs[0] = "first_frame"
    if mode in ("l2va", "fl2va"):
        refs[timeline["delivery_frames"] - 1] = "last_frame"
    guides = copy.deepcopy(shot["guides"])
    parent = None
    displayed = None
    take = doc["takes"].get(shot.get("parent_take"))
    settings = a.get("settings", {})
    if not isinstance(settings, dict):
        raise ValueError("H3 settings must be a JSON object.")
    settings = dict(settings)
    if shot.get("branch_action") == "continue":
        start = shot.get("start_mode")
        if start in ("motion", "original_motion"):
            parent = _parent_spec(p, doc, take, start, paths)
        elif start in ("visual", "regenerate"):
            first = _ending_guide(p, take, shot, ending_file)
            displayed = first.get("asset")
            guides = _visual_guides(guides,first,mode)
            if start == "regenerate":
                settings.update(save_mmh3=True, align_motion_ending=True)
        else:
            raise ValueError("Choose how to continue from the displayed ending.")
    expanded = []
    for g in guides:
        expanded.append(g)
        if g.get("audio_asset") and g.get("asset") != g["audio_asset"]:
            expanded.append({"asset": g["audio_asset"], "frame": g["frame"], "role": "audio", "usage": "timed"})
    found = set()
    for i, guide in enumerate(expanded):
        kind = _kind(guide)
        if kind not in ("image", "video", "audio"):
            raise ValueError("Choose an image, clip or audio guide.")
        rev = guide.get("revision")
        source = rev or guide.get("asset") or guide.get("audio_asset")
        # A revision may be used more than once. Keep each role's identity.
        key = f"{i}-{source}"
        path = ending_file if displayed and source == displayed else (studio._revision(p, rev) if rev else p.asset_path(source))
        paths[key] = path
        usage = guide.get("usage", "both")
        if usage not in ("timed", "reference", "both"):
            raise ValueError("Guide usage must be timed, reference or both.")
        role = refs.get(guide["frame"]) if kind == "image" else None
        if mode == "ref2va" and usage != "timed":
            role = "reference_" + kind
        if role:
            assets.append({"id": key, "role": role, "media_type": kind, "name": path.name, "revision": rev,
                           "audio_enabled": bool(guide.get("with_audio")), "semantic_role": guide.get("semantic_role", "other"),
                           **{k: guide[k] for k in ("clip_start_seconds","clip_end_seconds","prompt_tag") if k in guide}})
            found.add(role)
        if not role or (mode == "ref2va" and usage != "reference"):
            timed.append({"asset_id": key, "kind": kind, "frame": guide["frame"], "revision": rev,
                          "role": guide.get("role", kind), "with_audio": guide.get("with_audio", False)})
    missing = set(refs.values()) - found
    if missing:
        raise ValueError("Place a guide for " + ", ".join(sorted(missing)) + " or select another H3 mode.")
    spec = {"title": shot["name"], "project_id": doc["id"], "comfy_url": local_url(studio.prefs["video_url"]),
            "prompt": a["prompt"], "duration": shot["duration"], "seed": a.get("seed", 0), "mode": mode,
            "assets": assets, "guides": timed, "settings": settings, "parent": parent}
    bundle = generation.prepare_take(spec, paths.__getitem__)
    if displayed:
        p.add_asset(ending_file)
    return bundle, guides


def _archive_result(project, path, digest):
    if not digest:
        raise ValueError("The renderer output has no verified checksum.")
    source = Path(path)
    with source.open("rb") as stream:
        asset = project.store_stream(stream, source.name, expected_hash=digest)
    with project.transaction() as doc:
        doc["assets"].setdefault(asset["id"], asset)
    return asset["id"]


def engine_action(studio,p,op,a):
    doc = p.state()
    if op.startswith("recipe_"):
        from .recipes import action as recipe_action
        return recipe_action(p,op,a)
    if op == "director" or op.startswith("director_"):
        from .director import action as director_action
        return director_action(studio,p,op,a)
    if op in ("prepare_master","submit_master","export_master"):
        from .assembly import master_action
        return master_action(studio,p,op,a)
    if op=='guard':
        return {'games':game_guard()}
    if op=='composition':
        comp = doc['compositions'][a['composition']]
        with p.transaction() as updated:
            if a['revision'] not in comp['revisions']:
                raise ValueError('Invalid revision for this composition.')
            updated['compositions'][comp['id']]['current'] = a['revision']
        return {}
    if op=='restore_cut':
        p.restore_cut(int(a['index']))
        return {}
    if op=='legacy_cut':
        p.open_legacy_cut(a['key'])
        return {}
    if op=='choose_start':
        p.choose_start(a['shot'],a['mode'],a.get('revision'))
        return {}
    if op=='guide_info':
        from .generation import _probe_guide, guide_layout
        shot=doc['shots'][a['shot']]
        motion = a.get('motion', shot.get('start_mode') in ('motion','original_motion'))
        timeline=generation.timing(shot['duration'],39 if motion else 0)
        if shot.get("start_mode") == "regenerate":
            offset = timeline["render_frames"] - timeline["delivery_frames"]
            timeline.update(first=offset,last=timeline["render_frames"]-1,trim=[offset,timeline["render_frames"]])
        opts = {**a.get('settings',{}),'experimental_preview':shot['duration']==3}
        dims_config = comfy_transfer._settings(dict(mode=a.get('mode','t2va'),duration=shot['duration']),opts)
        size = a.get('size') or [dims_config['width'],dims_config['height']]
        if motion and (parent := doc['takes'].get(shot.get('parent_take'),{}).get('packet')):
            size = [parent['width'],parent['height']]
        results=[]
        with tempfile.TemporaryDirectory() as folder:
            guides, displayed = shot['guides'], None
            ending_file = Path(folder)/'Displayed ending.png'
            if shot.get('start_mode') in ('visual','regenerate'):
                first = _ending_guide(p,doc['takes'].get(shot.get('parent_take')),shot,ending_file)
                displayed = first.get('asset')
                guides = _visual_guides(guides,first,a.get('mode','t2va'))
            for g in guides:
                asset = g.get('asset') or g.get('audio_asset')
                if displayed and asset == displayed:
                    path = ending_file
                else:
                    path = studio._revision(p,g['revision']) if g.get('revision') else p.asset_path(asset)
                details = _probe_guide(path,_kind(g),g.get('with_audio',False))
                results.append({**details, 'kind':_kind(g), **guide_layout(g['frame'], details['frames'], timeline,
                                details['dimensions'], size, details['audio_seconds'])})
        return {'timeline':timeline,'guides':results}
    if op=='prepare':
        from .recipes import recipe, take_source
        shot = copy.deepcopy(doc['shots'][a['shot']])
        replay = None
        choice = None
        if a.get('source_take'):
            replay = take_source(doc,shot,a['source_take'])
            original = replay['recipe']
            a = {**original,**a,'settings':{**original['settings'],**a.get('settings',{})}}
            shot.update(guides=replay['guides'],duration=replay['duration'],branch_action='replay')
            if motion := replay['parent']:
                shot.update(parent_take=motion['take_id'],branch_action='continue',
                            start_mode='original_motion' if motion.get('use_original_motion') else 'motion')
                live = _parent_spec(p,doc,doc['takes'][shot['parent_take']],shot['start_mode'],{})
                if any(k in motion and motion[k]!=live[k] for k in ('packet_id','sha256','latent_revision','trim')):
                    raise ValueError('This take no longer matches its original motion packet and trim.')
        elif a.get('start_mode'):
            choice = {'mode':a['start_mode'], 'revision':a.get('start_revision')}
            previous = {k:shot.get(k) for k in ('parent_take','start_mode','start_revision','guides')}
            p.apply_start(doc,shot,**choice)
            choice['revision'] = shot['start_revision']
        if not str(a.get('prompt','')).strip():
            raise ValueError('Enter a shot prompt.')
        bundle,guides = _video(studio,p,a,doc,shot)
        key = uuid.uuid4().hex
        with p.transaction() as s:
            plan = {'shot':shot['id'],'guides':guides,'bundle':bundle,'prompt_text':a['prompt'],'request':copy.deepcopy(a)}
            if choice:
                plan.update(start_choice=choice,previous_start=previous)
                bundle['manifest']['start_choice'] = choice
            if replay:
                plan['source_take'] = replay['id']
                bundle['manifest']['recipe_source_take'] = replay['id']
                if replay['start']:
                    bundle['manifest']['start_choice'] = replay['start']
                plan['render_parent_take'] = (replay['parent'] or {}).get('take_id') or doc['shots'][shot['id']].get('parent_take') or replay['source_parent_take']
            plan['recipe'] = recipe(a)
            s.setdefault('prepared',{})[key] = plan
        return {'plan':key,'manifest':bundle['manifest']}
    if op=='submit_prepared':
        plan = doc['prepared'][a['plan']]
        bundle = plan['bundle']
        if doc['shots'][plan['shot']].get('locked_take'):
            raise ValueError('Unlock this selected take before submitting a new render.')
        generation.validate_saved_take(copy.deepcopy(bundle))
        payload = {**bundle,'studio_project':doc['id'],'studio_shot':plan['shot'],'studio_guides':plan['guides'],'studio_plan':a['plan'],
                   'parent_take':plan.get('render_parent_take',(bundle['manifest'].get('parent') or {}).get('take_id') or doc['shots'][plan['shot']].get('parent_take'))}
        with p.transaction() as updated:
            saved = updated['prepared'][a['plan']]
            target = updated['shots'][saved['shot']]
            if target.get('locked_take'):
                raise ValueError('Unlock this selected take before submitting a new render.')
            if saved.get('start_choice') and not saved.get('approved_job'):
                if any(target.get(field) != value for field,value in saved['previous_start'].items()):
                    raise ValueError('The continuation start or its guides changed. Prepare the shot again.')
                p.apply_start(updated,target,**saved['start_choice'])
            # The project change rolls back on queue failure. A retry uses the same
            # durable job ID and cannot overwrite a subsequently approved start.
            queued = studio.queue.enqueue('video',payload,job_id=a['plan'])
            if 'recipe' in saved and not (saved.get('approved_job') or saved.get('source_take')):
                target['recipe'] = saved['recipe']
                updated.get('recipe_drafts',{}).pop(target['id'],None)
            saved['approved_job'] = queued['id']
        return queued
    if op=='finish':
        take=doc['takes'][a['take']]
        return studio.queue.enqueue('finish',dict(installation=studio.prefs['finishing_install'],
            input=str(p.asset_path(take['video_asset'])),settings=a['settings'],studio_project=doc['id'],
            delivery_trim=take['settings'].get('delivery_trim'),audio_route=take['settings'].get('audio_route'),
            studio_shot=take['shot'],parent_take=take['id'],studio_guides=take['guides']))
    if op=='image_ai':
        from .image_tools import prepare_image
        return prepare_image(studio,p,a,doc)
    if op=='custom':
        base=local_url(studio.prefs['video_url'] if a['kind']=='video' else studio.prefs['image_url'])
        graph=a['graph']
        with httpx.Client(trust_env=False,timeout=20) as c:
            info=c.get(base+'/object_info')
            info.raise_for_status()
            comfy_transfer._validate_graph(graph,info.json(),set())
        return studio.queue.enqueue(a['kind'],{'comfy_url':base,'prompt':graph,'studio_project':doc['id'],'studio_shot':a.get('shot'),
                                  'studio_guides':doc['shots'].get(a.get('shot'),{}).get('guides',[])})
    if op=='adopt':
        job=studio.queue.get(a['id'],full=True)
        pl,receipt=job['payload'],job['receipt']
        if pl.get("packet_master"):
            from .assembly import adopt_master
            return adopt_master(p,job)
        if job['status']!='succeeded' or pl.get('studio_project')!=doc['id']:
            raise ValueError('Open the project that owns this completed job.')
        if job['kind']=='director':
            from .director import adopt as adopt_director
            return adopt_director(p,job)
        for t in doc['takes'].values():
            if t['settings'].get('job_id')==job['id']:
                return {'take':t['id']}
        if job['kind']=='image':
            if pl.get("visual_director"):
                from .image_director import adopt
                return adopt(p,job)
            from .image_tools import adopt_image
            return adopt_image(studio,p,pl,receipt,job)
        packet=receipt.get('packet') or {}
        if job['kind']=='finish':
            result=receipt['result']
            video=_archive_result(p,result['output_path'],result.get('sha256'))
        else:
            candidates=[x for x in receipt.get('outputs',[]) if Path(x['path']).suffix.lower() in ('.mp4','.webm','.mkv','.mov','.avi','.m4v')]
            if len(candidates) != 1:
                raise ValueError('This take requires one uniquely identified video output.')
            result=candidates[0]
            video=_archive_result(p,result['path'],result.get('sha256'))
        m=pl.get('manifest',{})
        settings={**m,'job_id':job['id'],'kind':job['kind'],'workflow':pl.get('workflow'),'graph':pl.get('prompt'),
                  'motion_selector':packet.get('selector'),'motion_revision':packet.get('packet_revision'),'packet_id':packet.get('packet_id'),
                  'worker':receipt.get('worker'),'devices':receipt.get('devices')}
        if plan := doc.get('prepared',{}).get(pl.get('studio_plan')):
            settings['request'] = plan.get('request') or {'prompt':plan.get('prompt_text',''),'seed':m.get('seed',0),'mode':m.get('mode'),'settings':{k:m[k] for k in ('resolution','aspect_ratio','quality') if k in m}}
        if job['kind']=='finish':
            for key in ('delivery_trim','audio_route'):
                if pl.get(key) is not None:
                    settings[key] = pl[key]
            settings.update(finishing_requested=result.get('requested_settings',pl.get('settings')),
                            finishing_effective=result.get('settings'),devices=result.get('worker_devices',[]),
                            finishing_stages=result.get('engine_reports',{}))
            if result.get('report_sha256'):
                settings['finishing_report_asset'] = _archive_result(p,result['report_path'],result['report_sha256'])
            elif result.get('report_path'):
                settings['finishing_report'] = result['report_path']
        if packet and (not packet.get('path') or not packet.get('sha256')):
            raise ValueError('The renderer did not archive a verifiable motion packet for this take.')
        if packet:
            archived = _archive_result(p, packet['path'], packet['sha256'])
        else:
            archived = None
        if timeline:=m.get('timeline'):
            settings.update(render_trim=timeline['trim'],delivery_trim=[n/24 for n in timeline['trim']],overlap_frames=timeline['first'])
        return {'take':p.save_take(pl['studio_shot'],settings,video,archived,
                                   parent_take=pl.get('parent_take'),guides=pl.get('studio_guides'))}
    raise ValueError('Unknown studio action.')
