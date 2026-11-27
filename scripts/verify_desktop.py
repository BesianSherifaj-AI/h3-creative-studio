"""Exercise a running, explicitly selected Copper Orbit acceptance fixture via WebView2 CDP.

Optional QA dependency: playwright. Start Studio with WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS
set to --remote-debugging-port=PORT for this test session only.
"""
import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def run(args):
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp('http://127.0.0.1:'+str(args.port))
        page = next(page for ctx in browser.contexts for page in ctx.pages if page.title()=='H3 Creative Studio')
        page.wait_for_function("window.H3 && window.H3.get().doc")
        state = page.evaluate("H3.api('state')")
        if Path(state['folder']).resolve()!=args.project.resolve() or state['project']['name']!='Copper Orbit':
            raise RuntimeError('Choose the explicitly created Copper Orbit acceptance project.')
        evidence = args.evidence.resolve()
        evidence.mkdir(parents=True,exist_ok=True)
        errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        def click(selector):
            page.locator(selector).click(timeout=12000)
        def value(fn):
            return page.evaluate(fn)
        def wait_revision(old):
            page.wait_for_function('id => H3.get().rev.id !== id',arg=old,timeout=10000)
        def render():
            count=value('H3.get().shot.takes.length')
            click('#preview')
            page.wait_for_function("n => H3.get().shot.takes.length > n",arg=count,timeout=120000)
            page.wait_for_function("!document.querySelector('#player').hidden && document.querySelector('#player').readyState >= 2")
            return value('H3.get().take.video_asset')
        if args.reopened:
            assert value("H3.get().shot.takes.length")>=3
            click('#takeTab')
            page.wait_for_function("document.querySelector('#player').readyState >= 2")
            assert value("Math.round(document.querySelector('#player').duration*24)")==120
            page.screenshot(path=evidence/'reopened-window.png')
            return {'reopened':True,'frames':120,'shot':value('H3.get().shot.name'),'errors':errors}
        # Drive the same transform form and guide buttons used at the workstation.
        for pos,x in [('gFirst',-85),('gMiddle',32),('gLast',85)]:
            click('#canvasTab')
            click('[data-pane="layers"]')
            click('[data-layer="2"]')
            old=value('H3.get().rev.id')
            page.fill('#lx',str(x))
            click('#applyLayer')
            wait_revision(old)
            click('#guide')
            click('#'+pos)
            click('#dialogFoot .primary')
            page.wait_for_function("!document.querySelector('#dialog').open")
        assert value('H3.get().shot.guides.map(g=>g.frame)')==[0,60,119]
        page.screenshot(path=evidence/'layered-desktop.png')
        baseline=render()
        click('#canvasTab')
        click('[data-pane="layers"]')
        old=value('H3.get().rev.id')
        page.fill('#lx','-150')
        click('#applyLayer')
        wait_revision(old)
        click('#guide')
        click('#gMiddle')
        click('#dialogFoot .primary')
        page.wait_for_function("!document.querySelector('#dialog').open")
        moved=render()
        click('[data-g="1"]')
        page.get_by_role('button',name='Remove guide').click()
        page.wait_for_function("H3.get().shot.guides.length===2")
        omitted=render()
        assert len({baseline,moved,omitted})==3
        # Restore the three-guide cut as the version the user opens.
        takes=value('H3.get().shot.takes')
        click('[data-take="'+takes[-3]+'"]')
        page.wait_for_function('id=>H3.get().take.id===id',arg=takes[-3])
        guides=value('H3.get().take.guides')
        for g in guides:
            if g['frame']==60:
                page.evaluate("(g)=>H3.api('guide',{shot:H3.get().shot.id,frame:g.frame,revision:g.revision,role:g.role}).then(H3.refresh)",g)
        page.screenshot(path=evidence/'saved-take.png')
        return {'ui_layer_edits':4,'displayed_guides':[0,60,119],'decoded_frames':120,
                'baseline':baseline,'moved':moved,'omitted':omitted,'selected_take':takes[-3],'errors':errors}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,required=True)
    parser.add_argument('--project',type=Path,required=True)
    parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--reopened',action='store_true')
    args=parser.parse_args()
    receipt=run(args)
    if receipt['errors']:
        raise RuntimeError(receipt['errors'])
    print(json.dumps(receipt,indent=2))
