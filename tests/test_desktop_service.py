import http.cookiejar
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from h3studio.application import Studio
from h3studio.media import frame_index
from h3studio.server import Server


def test_project_shortcut_service_and_decoded_trim(tmp_path):
    from h3studio.demo import create_demo
    studio=Studio(tmp_path/'app',create_demo(tmp_path/'five-second-shot').folder)
    server=Server(studio)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        browser=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        with pytest.raises(urllib.error.HTTPError) as error:
            browser.open(server.origin+'/')
        assert error.value.code==403
        assert b'Creative Studio' in browser.open(server.url).read()
        def command(op,**fields):
            req=urllib.request.Request(server.origin+'/api',data=json.dumps(dict(op=op,**fields)).encode(),headers={'Content-Type':'application/json'})
            return json.load(browser.open(req))
        snap=command('state')['project']
        shot=snap['cut'][0]
        assert [g['frame'] for g in snap['shots'][shot]['guides']]==[0,60,119]
        task=command('storyboard',shot=shot,width=320,height=180)['task']
        studio.pool.submit(lambda:None).result(40)
        result=next(j for j in command('jobs')['editorial'] if j['id']==task)
        assert result['state']=='completed',result
        take=result['result']['take']
        edited=command('trim',take=take,start=1,end=2,audio=[])['take']
        cache=studio.file('take',edited)
        assert len(frame_index(cache))==24
        assert browser.open(server.origin+'/take/'+edited).headers.get('Content-Type')=='video/mp4'
        assert len(studio.project.state()['takes'])==2
    finally:
        server.shutdown()
        server.server_close()
        studio.close()


def test_startup_recovers_when_previous_project_moved(tmp_path):
    moved = str(tmp_path / 'relocated')
    (tmp_path/'preferences.json').write_text(json.dumps({'last_project':moved}))
    studio = Studio(tmp_path)
    try:
        assert studio.state()['project']['name']=='Tidal Relay'
        assert studio.state()['notice']
    finally:
        studio.close()
    with pytest.raises(ValueError):
        Studio(tmp_path,moved)


def test_soundtrack_is_portable_and_mixed_continuously_across_cut(tmp_path):
    import shutil
    import numpy as np
    from test_media import ff, audio, markers
    from h3studio.project import Project
    studio=Studio(tmp_path/'home')
    src, bed, output=tmp_path/'shot.mp4',tmp_path/'narration.wav',tmp_path/'film.mp4'
    try:
        studio.action('new',dict(name='Two cuts',path=tmp_path/'project'))
        markers(src)
        for _ in range(2):
            row=studio.action('import_clip',dict(path=src))
            studio.action('trim',dict(take=row['take'],start=0,end=1,audio=[1,None]))
        ff('-f','lavfi','-i','sine=frequency=1200:sample_rate=48000:duration=2','-c:a','pcm_s16le',bed)
        entry=studio.action('soundtrack_save',dict(path=bed,offset=.25,gain_db=-6,track=0))
        studio.action('soundtrack_save',dict(path=bed,offset=0,gain_db=0,track=1))
        shutil.copytree(tmp_path/'project',tmp_path/'clone')
        studio.action('open',dict(path=tmp_path/'clone'))
        src.unlink()
        bed.unlink()
        assert studio.project.state()['soundtracks'][entry['id']]['offset']==.25
        job=studio.action('export',dict(path=output,width=96,height=64,fps='24',color='bt709',audio_tracks=2))['task']
        studio.pool.submit(lambda:None).result(30)
        assert studio.tasks[job]['state']=='completed',studio.tasks[job]
        main,isolated=audio(output,0),audio(output,1)
        def amplitude(data,t,hz):
            t0=round(48000*t)
            window=data[t0:t0+4800]
            return abs(np.dot(window,np.exp(2j*np.pi*hz*np.arange(len(window))/48000)))*2/len(window)
        assert amplitude(main,.05,1200)<.003
        for t in (.3,.9,1.,1.45):
            assert .45<amplitude(main,t,1200)/amplitude(isolated,t,1200)<.55
            assert amplitude(main,t,337)>.04
        assert len(frame_index(output))==48
        studio.action('soundtrack_remove',dict(id=entry['id']))
        assert entry['id'] not in Project(tmp_path/'clone').state()['soundtracks']
    finally:
        studio.close()


def test_recipe_legacy_recovery_defaults_and_clone(tmp_path):
    import shutil
    from h3studio.project import Project
    from h3studio.recipes import action
    p=Project.create(tmp_path/'source','Film')
    one=p.add_shot('Original',5)
    two=p.add_shot('Second',5)
    data=dict(mode="ref2va",prompt="Moving reflections",seed=541,settings={"steps":8,"loras_by_mode":{"ref2va":[{"name":"B","strength":.5},{"name":"A","strength":1}],"i2va":[]}})
    with p.transaction() as doc:
        doc.setdefault('prepared',{})['old']={'shot':one,'request':data}
    assert action(p,'recipe_get',{'shot':one})['recipe']==data
    for op in ('recipe_default','recipe_preset'):
        action(p,op,{'shot':one,'recipe':data,'name':'Night'})
    assert action(p,'recipe_get',{'shot':two})['recipe']==data
    custom={**data,'seed':0,'prompt':'At dawn'}
    action(p,'recipe_save',{'shot':two,'recipe':custom})
    shutil.copytree(p.folder,tmp_path/'clone')
    clone=Project(tmp_path/'clone')
    saved=action(clone,'recipe_get',{'shot':two})
    assert saved['recipe']==custom and saved['default']==data
    action(clone,'recipe_save',{'shot':two,'recipe':saved['presets']['Night']})
    assert action(Project(clone.folder),'recipe_get',{'shot':two})['recipe']==data
