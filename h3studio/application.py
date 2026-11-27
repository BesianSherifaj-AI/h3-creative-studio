"""Desktop project commands. Every take stores the inputs used for that render."""
from __future__ import annotations

import concurrent.futures
import copy
import json
import os
import sqlite3
import time
import tempfile
import threading
import uuid
from pathlib import Path

from PIL import Image, ImageOps
from . import media
from .demo import create_demo
from .runtime import guard_engines, release_idle_peers
from .project import Project


def atomic_json(path, value):
    temp = path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
    temp.write_text(json.dumps(value, indent=2), encoding='utf-8')
    temp.replace(path)


class Studio:
    def __init__(self, home, project=None):
        from .jobs import Queue
        self.home = Path(home).resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.prefs_path = self.home / 'preferences.json'
        self.prefs = {'video_url':'http://127.0.0.1:8021','image_url':'http://127.0.0.1:8022',
                      'director_url':'http://127.0.0.1:1234','director_model':'', 'finishing_install':''}
        if self.prefs_path.is_file():
            self.prefs.update(json.loads(self.prefs_path.read_text(encoding='utf-8')))
        folder = project or self.prefs.get('last_project')
        demo = self.home / 'Projects' / 'Copper Orbit'
        self.startup_notice = ''
        if folder:
            try:
                self.project = Project(folder)
            except (ValueError, OSError, sqlite3.DatabaseError):
                if project:
                    raise
                self.startup_notice = 'The last project could not be opened. Use Open project to locate it.'
                folder = None
        if not folder:
            self.project = Project(demo) if (demo / Project.DATABASE).is_file() else create_demo(demo)
        self.queue = Queue(self.home / 'queue', before_dispatch=lambda url: release_idle_peers(self.home,url))
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.tasks = {}
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='editorial')
        self.runner = threading.Thread(target=self._tick, name='renderer', daemon=True)
        self.last_error = ''
        self.runner.start()

    def close(self):
        self.stop.set()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def _tick(self):
        last_guard = 0.0
        while not self.stop.wait(2):
            try:
                self.queue.tick()
                if time.monotonic() - last_guard >= 8:
                    guard_engines(self.home)
                    last_guard = time.monotonic()
            except Exception as e:
                self.last_error = str(e)

    def _switch(self, p):
        self.project = p
        path = str(p.folder)
        previous = self.prefs.get('last_project') or str(self.home / 'Projects' / 'Copper Orbit')
        recent = [path, previous, *self.prefs.get('recent_projects',[])]
        self.prefs['recent_projects'] = list(dict.fromkeys(recent))[:8]
        labels = dict(self.prefs.get('recent_labels',{}))
        labels[path] = p.state()['name']
        self.prefs['recent_labels'] = {key: labels[key] for key in self.prefs['recent_projects'] if key in labels}
        self.prefs['last_project'] = path
        atomic_json(self.prefs_path, self.prefs)

    def _revision(self, p, ident):
        cache = p.folder / 'preview-cache'
        if ident not in p.state()['revisions']:
            raise ValueError('Unknown composition revision.')
        cache.mkdir(exist_ok=True)
        path = cache / (ident+'.png')
        if not path.is_file():
            tmp = cache / (uuid.uuid4().hex+'.png')
            p.render_revision(ident).save(tmp)
            os.replace(tmp, path)
        return path

    def file(self, kind, ident):
        with self.lock:
            if kind=='take':
                from .playback import take_preview
                return take_preview(self.project,ident)
            return self.project.asset_path(ident) if kind=='asset' else self._revision(self.project, ident)

    def state(self):
        return dict(project=self.project.state(), folder=str(self.project.folder), preferences=self.prefs, notice=self.startup_notice)

    def _tasks(self):
        return [{'id':i, **{k:v for k,v in info.items() if k!='cancel'}} for i,info in reversed(list(self.tasks.items()))]

    def _background(self, name, fn):
        ident = uuid.uuid4().hex
        cancel = threading.Event()
        info = {'name':name, 'state':'queued', 'cancel':cancel}
        self.tasks[ident] = info
        def work():
            info['state'] = 'running'
            try:
                if cancel.is_set():
                    raise InterruptedError('Cancelled.')
                info['result'] = fn(cancel.is_set)
                info['state'] = 'completed'
            except Exception as e:
                info.update(state='cancelled' if cancel.is_set() else 'failed', error=str(e))
        self.pool.submit(work)
        return {'task':ident}

    def action(self, op, a):
        with self.lock:
            p = self.project
            if op == 'state':
                return self.state()
            if op == 'jobs':
                return {'editorial':self._tasks(), 'gpu':self.queue.list(), 'error':self.last_error}
            if op == 'cancel':
                if a['id'] in self.tasks:
                    self.tasks[a['id']]['cancel'].set()
                else:
                    self.queue.cancel(a['id'])
                return {}
            if op in ('engine_status','start_engine','stop_engine','engine_config','engine_settings'):
                from .engine_controls import engine_action
                return engine_action(self,op,a)
            if op == 'settings':
                keys = ('video_url','image_url','director_url','director_model','finishing_install')
                self.prefs.update({k:str(a[k]).strip() for k in keys if k in a})
                atomic_json(self.prefs_path, self.prefs)
                return self.state()
            if op in ('new','open'):
                self._switch(Project.create(a['path'], a['name']) if op=='new' else Project(a['path']))
                return self.state()
            if op == 'recover':
                from .recovery import import_legacy
                return self._background('Import legacy project',lambda cancel: import_legacy(p,a['path'], media_root=a.get('media_root'), cancelled=cancel))
            if op == 'relink':
                from .recovery import relink
                return relink(p, a['missing'], a['path'])
            if op == 'image':
                path = Path(a['path'])
                with Image.open(path) as raw:
                    size = ImageOps.exif_transpose(raw).size
                asset = p.add_asset(path)
                new = dict(asset=asset, name=path.stem, visible=True, x=0,y=0,scale=1,rotation=0,opacity=1)
                if a.get('revision'):
                    old = p.state()['revisions'][a['revision']]
                    rev = p.save_composition(old['name'],old['width'],old['height'],old['layers']+[new],old['composition'], base_revision=old['id'])
                else:
                    rev = p.save_composition(path.stem,*size,[new])
                return {'revision':rev}
            if op == 'layer':
                r = p.state()['revisions'][a['revision']]
                layers = r['layers']
                i = a['index']
                if not 0 <= i < len(layers):
                    raise ValueError('Select a layer first.')
                if a.get('mask_path'):
                    with Image.open(a['mask_path']) as mask, Image.open(p.asset_path(layers[i]['asset'])) as base:
                        if ImageOps.exif_transpose(mask).size != ImageOps.exif_transpose(base).size:
                            raise ValueError('Choose a mask with the same dimensions as the layer.')
                    layers[i]['mask'] = p.add_asset(a['mask_path'])
                for k in ('x','y','scale','rotation','opacity','visible','blend','name'):
                    if k in a:
                        layers[i][k] = a[k]
                if a.get('remove_mask'):
                    layers[i].pop('mask',None)
                if a.get('move') in (-1,1):
                    j = max(0, min(len(layers)-1, i+a['move']))
                    layers.insert(j,layers.pop(i))
                return {'revision':p.save_composition(r['name'],r['width'],r['height'],layers,r['composition'],base_revision=r['id'])}
            if op == 'shot':
                return {'shot':p.add_shot(a['name'], float(a.get('duration',5)))}
            if op in ('guide','unguided'):
                guides = p.state()['shots'][a['shot']]['guides']
                role = a.get('role','image')
                frame = int(a['frame'])
                guides = [g for g in guides if (g['frame'],g.get('role','image'))!=(frame,role)]
                if op=='guide':
                    usage = a.get('usage','both')
                    if usage not in ('reference','timed','both'):
                        raise ValueError('Choose a valid guide use.')
                    g = {'frame':frame,'role':role,'usage':usage}
                    if a.get('revision'):
                        g['revision'] = a['revision']
                    else:
                        g['asset'] = p.add_asset(a['path'])
                        g['with_audio'] = bool(a.get('with_audio'))
                    guides.append(g)
                p.set_guides(a['shot'], sorted(guides,key=lambda g:g['frame']))
                return {}
            if op=='select':
                p.select_take(a['shot'],a['take'])
                return {}
            if op=='branch':
                return {'shot':p.branch_take(a['take'],a['action'],a.get('name'))}
            if op == 'storyboard':
                shot = p.state()['shots'][a['shot']]
                guides = copy.deepcopy(shot['guides'])
                visual = [dict(frame=g['frame'],path=self._revision(p,g['revision']) if g.get('revision') else p.asset_path(g['asset']))
                          for g in guides if g.get('role','image')=='image']
                def draw(cancel):
                    with tempfile.TemporaryDirectory() as tmp:
                        path = Path(tmp)/'Storyboard.mp4'
                        receipt = media.render_storyboard(visual,path,frames=round(shot['duration']*24),
                                                          width=int(a.get('width',960)),height=int(a.get('height',540)),cancel=cancel)
                        receipt.pop('path',None)
                        asset = p.add_asset(path)
                        return {'take':p.save_take(shot['id'],receipt,asset,guides=guides)}
                return self._background('Render storyboard · '+shot['name'],draw)
            if op == 'import_clip':
                asset = p.add_asset(a['path'])
                meta = media.probe_media(p.asset_path(asset))
                options = dict(kind='import',source=meta)
                if Path(a['path']).suffix.lower() not in ('.mp4','.webm') or meta.get('pix_fmt') not in ('yuv420p','yuvj420p'):
                    from .editor import playback_proxy
                    with tempfile.TemporaryDirectory() as tmp:
                        preview = Path(tmp)/'Playback proxy.mp4'
                        playback_proxy(p.asset_path(asset),preview)
                        options['preview_asset'] = p.add_asset(preview)
                shot = p.add_shot(Path(a['path']).stem,meta['duration'])
                take = p.save_take(shot,options,asset)
                return {'shot':shot,'take':take}
            if op == 'trim':
                take = p.state()['takes'][a['take']]
                pth = p.asset_path(take['video_asset'])
                duration = media.probe_media(pth)['duration']
                start,end = float(a['start']),float(a['end'])
                if not 0<=start<end<=duration+1e-6:
                    raise ValueError('Trim must fall inside the source.')
                opts = {**take['settings'],'delivery_trim':[start,end],'edited_ending':True,'audio_route':a.get('audio',[])}
                if parent_job := opts.pop('job_id',None):
                    opts['source_job_id'] = parent_job
                return {'take':p.save_take(take['shot'],opts,take['video_asset'],take.get('packet_asset'),take['id'],guides=take['guides'])}
            if op in ('frame','frame_branch','edit_anchor','edit_range','edit_info','paint_layer'):
                from .editor import editor_action
                return editor_action(self,p,op,a)
            if op == 'cut':
                with p.transaction() as doc:
                    order = a['order']
                    if set(order) != set(doc['cut']) or len(order)!=len(set(order)):
                        raise ValueError('Timeline order changed. Reload and retry.')
                    p._remember_cut(doc)
                    doc['cut']=order
                return {}
            if op == 'export':
                doc = p.state()
                clips = []
                for shot in doc['cut']:
                    t = doc['takes'].get(doc['shots'][shot]['selected_take'])
                    if not t or not t.get('video_asset'):
                        raise ValueError('Render a take for '+doc['shots'][shot]['name']+' before exporting.')
                    options = t['settings']
                    item = {'path':str(p.asset_path(t['video_asset']))}
                    if 'delivery_trim' in options:
                        item['start'],item['end'] = options['delivery_trim']
                    if options.get('audio_route'):
                        item['audio'] = options['audio_route']
                    clips.append(item)
                out = Path(a['path'])
                if out.exists():
                    raise ValueError('Choose a new export filename to preserve the existing film.')
                return self._background('Export film',lambda cancel: media.export_film(clips,out,
                    width=int(a['width']),height=int(a['height']),fps=str(a['fps']),color=a['color'],
                    audio_tracks=int(a['audio_tracks']),cancel=cancel))
            from .integrations import engine_action
            return engine_action(self, p, op, a)
