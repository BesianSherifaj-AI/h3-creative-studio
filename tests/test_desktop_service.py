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
    studio=Studio(tmp_path/'app')
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
        assert studio.state()['project']['name']=='Copper Orbit'
        assert studio.state()['notice']
    finally:
        studio.close()
    with pytest.raises(ValueError):
        Studio(tmp_path,moved)
