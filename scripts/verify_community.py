"""Run the Windows community workspace smoke in a new home and save native WebView2 evidence."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import wave

import av
import numpy as np
import psutil
from playwright.sync_api import sync_playwright


def connect_desktop(pw, port, launcher, home):
    """Wait for the packaged window and report the observed process state on startup failure."""
    deadline = time.monotonic() + 80
    last_error = "Waiting for the WebView2 debugging port."
    while launcher.poll() is None and time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=.5):
                pass
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=4000)
            pages = [page for context in browser.contexts for page in context.pages]
            if pages:
                return pages[0]
            last_error = "WebView2 responded before its page was ready."
        except Exception as error:
            last_error = str(error).splitlines()[0]
        time.sleep(.35)
    processes = []
    try:
        parent = psutil.Process(launcher.pid)
        processes = [(child.name(), child.pid, child.status()) for child in parent.children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    log = Path(home)/"desktop.log"
    diagnostic = dict(launcher_exit=launcher.poll(), launcher_pid=launcher.pid, child_processes=processes,
                      port=port, last_error=last_error, desktop_log=log.is_file())
    if log.is_file():
        diagnostic["startup_log_tail"] = log.read_text(encoding="utf-8", errors="replace")[-2200:]
    raise RuntimeError("Studio desktop startup failed. " + json.dumps(diagnostic))


def close_window(port):
    listeners = {c.pid for c in psutil.net_connections(kind="tcp") if c.laddr.port == port and c.status == psutil.CONN_LISTEN}
    candidates = set()
    for pid in listeners:
        process = psutil.Process(pid)
        candidates.update(p.pid for p in process.parents() if p.name().lower() in ("pythonw.exe","h3 creative studio.exe"))
    def close(hwnd, _):
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
        title = ctypes.create_unicode_buffer(100)
        ctypes.windll.user32.GetWindowTextW(hwnd,title,100)
        if pid.value in candidates and title.value == "H3 Creative Studio":
            ctypes.windll.user32.PostMessageW(hwnd,0x10,0,0)
        return True
    hook = ctypes.WINFUNCTYPE(ctypes.c_bool,ctypes.c_void_p,ctypes.c_void_p)(close)
    ctypes.windll.user32.EnumWindows(hook,0)


def pick_path(page, path):
    # Supply the destination a user would select in the native Windows file dialog.
    page.evaluate("file=>{window.pywebview.api.choose=async()=>file;}", str(path))


def export_film(page, output, width, height, *, open_dialog=True):
    # A prior export in the same session can already be completed, even before the new job exists.
    known = page.evaluate("H3.api('jobs').then(j=>j.editorial.map(t=>t.id))")
    if open_dialog:
        page.click("#export")
    page.fill("#ew", str(width))
    page.fill("#eh", str(height))
    pick_path(page, output)
    page.get_by_role("button", name="Choose export file").click()
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        job = page.evaluate("ids=>H3.api('jobs').then(j=>j.editorial.find(t=>t.name==='Export film'&&!ids.includes(t.id)))", known)
        if job and job["state"] in ("completed","failed","cancelled"):
            assert job["state"] == "completed", job
            return job["id"]
        time.sleep(.25)
    raise RuntimeError("The new film export did not complete.")


def numbered_video(path, ids):
    # Six wide black/white stripes encode the original frame number, surviving normal H.264 compression.
    with av.open(str(path), "w") as container:
        stream = container.add_stream("libx264", rate=24)
        stream.width, stream.height, stream.pix_fmt = 128, 72, "yuv420p"
        for index in ids:
            image = np.full((72,128,3), [30,62,100], dtype=np.uint8)
            for bit in range(6):
                image[16:48,bit*20+2:bit*20+17] = 242 if index & (1<<bit) else 8
            frame = av.VideoFrame.from_ndarray(image, format="rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
def edit_fixtures(folder):
    leader, source = folder / "Leader A.mp4", folder / "Numbered frames.mp4"
    numbered_video(leader, [55]*12)
    numbered_video(source, range(48))
    sound = folder / "Timing tone.wav"
    with wave.open(str(sound), "wb") as out:
        out.setparams((1,2,48000,0,"NONE","not compressed"))
        samples = np.round(6000*np.sin(2*np.pi*880*np.arange(24000)/48000)).astype("<i2")
        out.writeframes(samples.tobytes())
    return leader, source, sound


def cut_ready(page):
    page.wait_for_function("""()=>{
        const clips=[...document.querySelectorAll('#cutPicture .cut-filmstrip')];
        return document.querySelector('#cutVideo')?.readyState>=2 && clips.length===3 &&
            clips.every(strip=>strip.children.length===3 && [...strip.children].every(img=>img.complete && img.naturalWidth>0)) &&
            document.querySelectorAll('#cutAudioLane svg').length===3 && document.querySelector('#cutBeds svg');
    }""", timeout=45000)


def verify_focus(page, shot, pixel_id=None):
    clip = next(c for c in page.evaluate("H3.api('cut_view')")["clips"] if c["shot"] == shot)
    frame = clip["start_frame"]
    clock = f"{frame//1440:02d}:{(frame//24)%60:02d}:{frame%24:02d}"
    page.wait_for_function("""({shot,take,name,frame,clock})=>{
        const q=s=>document.querySelector(s), v=q('#cutVideo');
        const offset=q('#cutHead')?.getBoundingClientRect().left-q('#cutRuler')?.getBoundingClientRect().left;
        return q('#cutPicture .chosen')?.dataset.cutShot===shot && q('#cutMonitorLabel').textContent===name &&
            q('#cutProperties h3').textContent===name && q('#cutClock').textContent.startsWith(clock+' / ') &&
            Math.abs(offset-frame*Number(q('#cutZoom').value)/24)<3 &&
            new URL(v.currentSrc || location.href).pathname==='/take/'+take && v.readyState>=2 &&
            v.paused && !v.seeking && v.currentTime<.001;
    }""", arg={**clip, "frame":frame, "clock":clock})
    if pixel_id is not None:
        page.wait_for_function("""expected=>{
            const v=document.querySelector('#cutVideo'),c=document.createElement('canvas');
            c.width=v.videoWidth;c.height=v.videoHeight;const ctx=c.getContext('2d');ctx.drawImage(v,0,0);
            let id=0;for(let b=0;b<6;b++)if(ctx.getImageData(b*20+10,32,1,1).data[0]>128)id+=2**b;
            return id===expected;
        }""", arg=pixel_id)
    return dict(shot=shot, take=clip["take"], label=clip["name"], timeline_frame=frame, clock=clock,
                monitor_source=page.locator("#cutVideo").get_attribute("src"), visible_pixel_id=pixel_id)


def verify_director_handoffs(page):
    page.click('[data-room-select="film"]')
    page.click('[data-dr="row"][data-v="1"]')
    timeline = page.locator('[data-dr="timeline"]')
    selected = timeline.get_attribute("data-v")
    timeline.click()
    film = verify_focus(page, selected)
    assert film["timeline_frame"] > 0
    page.click('[data-room-select="game"]')
    page.click('[data-dr="gameVideo"]')
    page.wait_for_selector("#hm")
    page.click("#closeDialog")
    page.click("#guide")
    page.click("#gFirst")
    page.locator("#dialogFoot").get_by_role("button", name="Place guide").click()
    page.click("#preview")
    page.wait_for_function("H3.get().shot?.game_turn && H3.get().take?.video_asset", timeout=90000)
    page.click('[data-room-select="game"]')
    selected = timeline.get_attribute("data-v")
    timeline.click()
    game = verify_focus(page, selected)
    assert game["timeline_frame"] > film["timeline_frame"] and game["shot"] != film["shot"]
    return dict(film=film, game=game)


def verify_edit_workflow(page, evidence):
    leader, source, music = edit_fixtures(evidence)
    project_dir = evidence / "Imported footage project"
    output = evidence / "Imported and scored.mp4"
    page.click("#projectMenu")
    page.click("#newProject")
    page.fill("#projectTitle", "Imported footage check")
    pick_path(page, project_dir)
    page.get_by_role("button", name="Choose folder").click()
    page.wait_for_function("H3.get().doc.name==='Imported footage check' && !document.querySelector('#dialog').open")
    page.click('[data-room-select="edit"]')
    pick_path(page, leader)
    page.click("#cutImport")
    page.wait_for_function("document.querySelector('#cutPicture [data-cut-shot]') && H3.get().doc.cut.length===1")
    a = page.evaluate("H3.get().doc.cut[0]")
    focus_a = verify_focus(page, a, 55)
    initial_a = page.evaluate("id=>H3.get().doc.shots[id]", a)
    pick_path(page, source)
    page.click("#cutImport")
    page.wait_for_function("document.querySelectorAll('#cutPicture [data-cut-shot]').length===2 && H3.get().doc.cut.length===2")
    b = page.evaluate("H3.get().doc.cut[1]")
    focus_b = verify_focus(page, b, 0)
    assert focus_b["timeline_frame"] == 12
    page.fill("#cutIn", "0.25")
    page.fill("#cutOut", "1.75")
    page.click("#cutApply")
    page.wait_for_function("document.querySelector('#cutSummary').textContent.includes('00:02:00')")
    trimmed = verify_focus(page, b, 6)
    doc = page.evaluate("H3.get().doc")
    assert doc["shots"][a] == initial_a
    assert doc["takes"][trimmed["take"]]["parent_take"] == focus_b["take"]
    assert doc["takes"][trimmed["take"]]["settings"]["delivery_trim"] == [.25,1.75]
    # Split B on delivery frame 24 so the output crosses a cut in the middle of the tone.
    ruler = page.locator("#cutRuler").bounding_box()
    zoom = int(page.input_value("#cutZoom"))
    page.mouse.click(ruler["x"] + zoom, ruler["y"] + ruler["height"]/2)
    page.click("#cutSplit")
    page.wait_for_function("document.querySelectorAll('#cutPicture [data-cut-shot]').length===3")
    page.click("#cutScore")
    page.get_by_role("button", name="Add soundtrack").click()
    pick_path(page, music)
    page.click("#bedFile")
    page.fill("#bedOffset", "0.875")
    page.get_by_role("button", name="Save to film").click()
    page.wait_for_function("document.querySelector('#dialogTitle').textContent==='Film soundtrack' && document.querySelector('[data-bed]')")
    page.click("#closeDialog")
    cut_ready(page)
    page.screenshot(path=evidence/"Edit-import-and-mix.jpg", type="jpeg", quality=90)
    job = export_film(page, output, 128, 72)
    with av.open(str(output)) as film:
        video = film.streams.video[0]
        assert (video.width,video.height) == (128,72) and video.average_rate == 24
        frame_ids, clock = [], []
        for frame in film.decode(video=0):
            pixels = frame.to_ndarray(format="rgb24")
            frame_ids.append(sum(1<<bit for bit in range(6) if pixels[32,bit*20+10].mean()>128))
            clock.append(float(frame.pts*frame.time_base))
    assert frame_ids == [55]*12+list(range(6,42)), frame_ids
    assert np.allclose(clock, np.arange(48)/24, rtol=0, atol=.000002), clock
    with av.open(str(output)) as film:
        stream = film.streams.audio[0]
        assert stream.rate == 48000
        audio = np.concatenate([f.to_ndarray()[0] for f in film.decode(audio=0)])
    def peak(start, end):
        return float(np.max(np.abs(audio[round(start*48000):round(end*48000)])))
    before, active, after = peak(.02,.85), peak(.9,1.35), peak(1.4,1.99)
    assert before<.001 and active>.075 and after<.001, (before,active,after)
    window = audio[48000:48000+8192]
    dominant = np.argmax(abs(np.fft.rfft(window*np.hanning(len(window)))))*48000/len(window)
    assert abs(dominant-880)<4, dominant
    state = page.evaluate("H3.api('state')")
    doc = state["project"]
    assert Path(state["folder"]).resolve() == project_dir.resolve()
    assert doc["shots"][a] == initial_a
    trims = [doc["takes"][doc["shots"][s]["selected_take"]]["settings"].get("delivery_trim") for s in doc["cut"]]
    assert trims[1:] == [[.25,.75],[.75,1.75]], trims
    return dict(project=doc["name"],shots=len(doc["cut"]),source_trim_seconds=trims,import_a=focus_a,import_b=focus_b,
                trimmed_b=trimmed,preserved_a=initial_a["selected_take"],
                native_picker="supplied fixture paths", export_job=job,output=output.name,
                decoded_frames=len(frame_ids),source_frame_ids=frame_ids,delivery_fps=24,
                soundtrack_offset=.875,soundtrack_duration=.5,soundtrack_hz=float(dominant),
                audio_peaks=dict(before=before,during=active,after=after))


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe",type=Path,help="Community launcher. Defaults to the current source checkout.")
    parser.add_argument("--evidence",type=Path,required=True)
    parser.add_argument("--isolated-path",action="store_true",help="Start with Windows system tools only and install private media tools from the UI.")
    parser.add_argument("--edit-workflow", action="store_true", help="Import numbered footage in Edit, trim and split it, add an audio track and verify the decoded export.")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    args.evidence.mkdir(parents=True,exist_ok=True)
    evidence = Path(tempfile.mkdtemp(prefix="Studio-",dir=args.evidence)).resolve()
    print(evidence,flush=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        port = sock.getsockname()[1]
    env = {**os.environ,"WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS":f"--remote-debugging-port={port}"}
    if args.isolated_path:
        system_root = Path(os.environ["SystemRoot"])
        env["PATH"] = os.pathsep.join([str(system_root/"System32"), str(system_root)])
    cmd = [str(args.exe.resolve())] if args.exe else [str(root/".venv/Scripts/pythonw.exe"),str(root/"studio.py")]
    launcher = subprocess.Popen([*cmd,"--home",str(evidence/"home")],env=env,cwd=evidence)
    errors = []
    try:
        with sync_playwright() as pw:
            page = connect_desktop(pw, port, launcher, evidence/"home")
            page.on("pageerror",lambda error:errors.append(str(error)))
            page.wait_for_function("window.H3?.get().doc?.name==='Tidal Relay' && typeof window.pywebview?.api?.choose==='function'")
            try:
                page.wait_for_selector("#motionPrompt")
            except Exception:
                page.screenshot(path=evidence/"startup-failure.jpg",type="jpeg",quality=90)
                print(dict(errors=errors, room=page.evaluate("document.body.dataset.room"),
                           inspector=page.locator("#pane").inner_text()), flush=True)
                raise
            page.wait_for_function("document.querySelector('#canvasImage').naturalWidth===1600 && document.fonts.status==='loaded'")
            assert len(page.input_value("#motionPrompt")) > 35
            assert page.locator(".motion-guides img").count() == 3
            page.screenshot(path=evidence/"Motion.jpg",type="jpeg",quality=90)
            page.click("#engines")
            page.wait_for_selector("#image-requirements")
            page.fill("#imageUrl","http://127.0.0.1:1")
            page.click("#image-requirements")
            page.wait_for_function("document.querySelectorAll('#image-report details').length===10")
            page.locator("#image-report summary").first.click()
            weight=page.locator("#image-report [data-model-file]").first
            assert weight.is_visible() and "GB" in weight.inner_text()
            assert "offline" in page.locator("#image-report p").inner_text().lower()
            model_links=page.locator("#image-report [data-model-file]").count()
            page.click("#closeDialog")
            media_setup = None
            if args.isolated_path:
                assert not page.evaluate("H3.api('community_status')")["media"]["ready"]
                page.click('[data-room-select="edit"]')
                page.wait_for_function("""()=>document.querySelector('#cutUnavailable')?.textContent &&
                    document.querySelector('#cutApply')?.disabled && !document.querySelector('#cutVideo').hasAttribute('src')""")
                unprobed = page.evaluate("H3.api('cut_view')")
                assert all(not c["asset"] and c.get("error") for c in unprobed["clips"])
                page.click("#help")
                page.click("#getMediaTools")
                deadline = time.monotonic() + 240
                while time.monotonic() < deadline:
                    media = page.evaluate("H3.api('community_status')")["media"]
                    if media["ready"] and media["source"] == "private":
                        break
                    time.sleep(.5)
                else:
                    raise RuntimeError("Studio's private media installation did not finish.")
                if page.locator("dialog").evaluate("e=>e.open"):
                    page.click("#closeDialog")
                cut_ready(page)
                assert page.evaluate("document.body.dataset.room") == "edit"
                assert page.is_enabled("#cutApply") and not page.locator("#cutUnavailable").inner_text()
                probed = page.evaluate("H3.api('cut_view')")
                assert (probed["version"],probed["project"]) == (unprobed["version"],unprobed["project"])
                initial_focus = verify_focus(page, probed["clips"][0]["shot"])
                page.click("#cutNext")
                page.wait_for_function("document.querySelector('#cutClock').textContent.startsWith('00:00:01')")
                page.click("#cutPrev")
                page.screenshot(path=evidence/"Edit-after-media-install.jpg",type="jpeg",quality=90)
                media_setup = dict(same_version=probed["version"],room="edit",before_errors=[c["error"] for c in unprobed["clips"]],
                                   enabled_trim=True,thumbnail_images=page.locator("#cutPicture img").count(),
                                   clip_waveforms=page.locator("#cutAudioLane svg").count(),monitor=initial_focus)
            for room,selector in (("image","#imageRoomCanvas"),("film","#directorWorkspace"),("game","#directorWorkspace"),("edit","#cutVideo")):
                page.click('[data-room-select="'+room+'"]')
                page.wait_for_selector(selector)
                if room in ("film","game"):
                    page.wait_for_function("document.querySelector('#directorWorkspace').innerText.trim().length>100")
                if room=="image":
                    page.wait_for_function("document.querySelector('#imageRoomCanvas').width>300")
                if room=="edit":
                    cut_ready(page)
                page.wait_for_timeout(420)
                page.screenshot(path=evidence/(room.title()+".jpg"),type="jpeg",quality=90)
            output = evidence/"Tidal Relay.mp4"
            export_film(page,output,640,360)
            with av.open(str(output)) as video:
                decoded = sum(1 for _ in video.decode(video=0))
            with av.open(str(output)) as audio:
                peaks = [float(np.max(np.abs(frame.to_ndarray().astype(float)))) for frame in audio.decode(audio=0)]
            assert decoded==240 and peaks and max(peaks)>.01
            handoffs = verify_director_handoffs(page) if args.edit_workflow else None
            edit_result = verify_edit_workflow(page,evidence) if args.edit_workflow else None
            assert not errors,errors
            status = page.evaluate("H3.api('community_status')")
            (evidence/"verification.json").write_text(json.dumps(dict(launcher=cmd[0],frames=decoded,audio_peak=max(peaks),media=status["media"],
                errors=errors,image_model_links=model_links,media_setup_in_open_edit=media_setup,director_timeline=handoffs,edit_workflow=edit_result),indent=2))
    finally:
        close_window(port)


if __name__=="__main__":
    run()
