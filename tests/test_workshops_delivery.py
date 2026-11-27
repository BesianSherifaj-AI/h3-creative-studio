from pathlib import Path

import av
import numpy as np
import pytest

from h3studio import media
from h3studio.delivery import write_captions
from h3studio.sample import open_example
from h3studio.workshops import catalog, open_lesson


def test_editable_lessons_reopen_in_their_own_project(tmp_path):
    original = open_example(tmp_path)
    before = original.state()
    cards = catalog()
    assert len(cards) == len({card["id"] for card in cards}) == 8
    assert all(card["image"].startswith("data:image/jpeg;base64,") for card in cards)
    project, route = open_lesson(tmp_path, "edit")
    shot = route["shot"]
    assert project.state()["shots"][shot]["name"] == "The receiver wakes"
    with project.transaction() as doc:
        doc["shots"][shot]["name"] = "My receiver"
        doc["cut"].remove(shot)
        doc["cut"].append(shot)
    reopened, reopened_route = open_lesson(tmp_path, "edit")
    assert reopened_route["shot"] == shot
    with reopened.transaction() as doc:
        del doc["example_shots"]
    assert open_lesson(tmp_path, "edit")[1]["shot"] == shot
    assert reopened.state()["shots"][shot]["name"] == "My receiver"
    assert original.state() == before
    with pytest.raises(ValueError):
        open_lesson(tmp_path, "../Original")


def test_portrait_center_fill_and_muxed_overlapping_captions(tmp_path):
    from PIL import Image
    src = tmp_path/"original.mp4"
    plate = tmp_path/"plate.png"
    Image.new("RGB", (160, 90), (180, 76, 36)).save(plate)
    media.render_storyboard([{"frame":0,"path":plate}], src, frames=24, width=160, height=90)
    clips = [{"path": src}]
    cues = [(50, 650, "Mara: Water"), (400, 1500, "Radio: Received"), (2000, 3000, "Outside")]
    for framing in ("fit", "fill"):
        path = tmp_path/(framing+".mp4")
        result = media.export_film(clips, path, width=90, height=160, framing=framing, captions=cues)
        assert result["captions"] == 3
        with av.open(str(path)) as video:
            frames = list(video.decode(video=0))
            assert len(frames) == 24
            top = frames[0].to_ndarray(format="rgb24")[3, 45]
            assert (np.max(top) < 12 if framing == "fit" else top[0] > 120)
        with av.open(str(path)) as video:
            assert video.streams.subtitles[0].codec_context.name == "mov_text"
            packets = [p for p in video.demux(subtitles=0) if p.size > 2]
            assert [round(p.pts*p.time_base*1000) for p in packets] == [50, 400, 650]
            assert b"Water" in bytes(packets[1]) and b"Received" in bytes(packets[1])
            assert round((packets[-1].pts+packets[-1].duration)*packets[-1].time_base*1000) == 1000
    with pytest.raises(ValueError, match="Center fill"):
        media.export_film(clips,tmp_path/"bad.mp4",framing="stretch")
    assert write_captions(Path(tmp_path/"empty.srt"), [(1100, 1200, "Offscreen")], 1) == 0


def test_delivery_adapts_saved_audio_routes_to_selected_outputs(tmp_path, monkeypatch):
    from h3studio.application import Studio
    from h3studio.project import Project
    p = Project.create(tmp_path / "film", "Mix")
    file = tmp_path / "clip.mp4"
    file.write_bytes(b"dispatch fixture")
    asset = p.add_asset(file)
    shot = p.add_shot("Harbor")
    p.save_take(shot, {"audio_route": [0]}, asset)
    with p.transaction() as doc:
        doc["soundtracks"] = {"v": {"id": "v", "asset": asset, "track": 1}}
    studio = Studio(tmp_path / "home", p.folder)
    monkeypatch.setattr(studio, "_background", lambda _, fn: fn(lambda: False))
    monkeypatch.setattr(media, "export_film", lambda clips, out, **settings: (clips, settings))
    try:
        args = dict(path=str(tmp_path / "out.mp4"), width=160, height=90, fps="24", color="bt709")
        clips, mix = studio.action("export", dict(args, audio_tracks=2))
        assert clips[0]["audio"] == [0, None]
        assert mix["soundtracks"][0]["track"] == 1
        clips, mix = studio.action("export", dict(args, audio_tracks=0))
        assert clips[0]["audio"] == [] and mix["soundtracks"] == []
    finally:
        studio.close()
