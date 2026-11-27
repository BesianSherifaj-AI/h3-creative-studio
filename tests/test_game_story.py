import copy
import shutil

import pytest

from h3studio import director, game_story
from h3studio.project import Project
from h3studio.sample import open_example


@pytest.fixture
def relay(tmp_path):
    p=open_example(tmp_path)
    root=p.state()["director"]["active_turn"]
    def call(op,turn=root,**args):
        return director.action(None,p,"director_story_"+op,dict(turn=turn,**args))
    return p,root,call


def test_authored_paths_legacy_reopen_and_branch_isolation(relay):
    project,root,call=relay
    with project.transaction() as doc:
        turn=doc["director"]["turns"][root]
        for name in ("story_plates","story_node","scene_visual"):
            turn.pop(name)
    assert len(call("view")["choices"])==3
    for first in ("answer","trace","shore"):
        ident=call("choose",choice=first)["turn"]
        for choice in call("view",ident)["choices"]:
            ending=call("choose",ident,choice=choice["id"])["turn"]
            snap=call("view",ending)
            assert snap["ending"] and not snap["choices"] and snap["objectives"][0]["status"]=="completed"
            cold=Project(project.folder).state()["director"]
            assert cold["active_turn"]==ending and cold["turns"][ending]["parent"]==ident
            assert call("choose",ident,choice=choice["id"])["turn"]==ending
            if first=="shore" or choice["id"]=="beacon":
                assert snap["inventory"]==["Field lamp"] and snap["place"]=="Breakwater"
    assert len(project.state()["director"]["turns"])==10
    assert call("view")["inventory"]==[] and call("view")["objectives"][0]["status"]=="active"
    with pytest.raises(ValueError,match="available move"):
        call("choose",choice="chart")


def test_waypoint_cold_reopen_and_branch_to_film(relay):
    project,root,call=relay
    originals=copy.deepcopy(project.state()["shots"])
    shore=call("choose",choice="shore")["turn"]
    scene=call("view",shore)
    take=scene["visual"]["take"]
    assert scene["visual"]["revision"] and scene["visual"]["video"]
    point=call("bookmark",shore,take=take,seconds=.75)["waypoint"]
    if shutil.which("ffprobe"):
        assert point["source_frame"]==18 and point["source_pts"] is not None
    with pytest.raises(ValueError,match="changed"):
        call("bookmark",shore,take="another",seconds=0)
    with pytest.raises(ValueError,match="visible time"):
        call("bookmark",shore,take=take,seconds=float("inf"))
    alternate=call("choose",choice="trace")["turn"]
    restored=director.action(None,Project(project.folder),"director_story_restore",dict(turn=alternate,waypoint=point["id"]))
    assert restored=={"turn":shore,"waypoint":point}
    assert call("view",shore)["visual"]["revision"]==point["visual"]["revision"]
    end=call("choose",shore,choice="beacon")["turn"]
    result=call("film",end)
    assert call("film",end)==result
    state=project.state()
    assert state["cut"]==list(originals)+result["shots"]
    assert [state["shots"][s]["game_turn"] for s in result["shots"]]==[root,shore,end]
    assert [state["shots"][s] for s in originals]==list(originals.values())
    for sid in result["shots"]:
        shot=state["shots"][sid]
        saved=state["takes"][shot["selected_take"]]
        source=state["takes"][saved["parent_take"]]
        assert saved["shot"]==sid and saved["video_asset"]==source["video_asset"]
        assert saved["guides"]==shot["guides"]==source["guides"]
    assert state["timeline_edits"][-1]["label"]=="Add Game branch to film"
    from h3studio import cut
    cut._mutate(project,"cut_undo",{"version":cut._version(state)})
    assert project.state()["cut"]==list(originals)
    assert call("film",end)==result
    assert project.state()["cut"]==state["cut"]
    assert project.state()["takes"]==state["takes"]


def test_film_branch_without_media_makes_editable_shots(tmp_path):
    project=Project.create(tmp_path/"project","Original game")
    with project.transaction() as doc:
        store=director._store(doc)
        ident=doc["id"]
        store["turns"][ident]=dict(parent=None,action="Investigate",scene="A wire hums.",
                                   actions=[],state_changes=[])
        store["active_turn"]=ident
    result=game_story.action(project,"director_story_film",dict(turn=ident))
    doc=project.state()
    assert doc["shots"][result["shots"][0]]["director_prompt"]=="A wire hums."
    assert not doc["shots"][result["shots"][0]]["selected_take"]


def test_narration_follows_promoted_occurrence_across_partial_restore_and_trim(relay,monkeypatch):
    from h3studio import cut
    p,root,call=relay
    lengths={p.asset_path(t["video_asset"]): t["duration"] for t in p.state()["takes"].values() if t.get("video_asset")}
    def sample_metadata(path):
        duration=lengths[path]
        return dict(duration_exact=str(duration),duration=duration,audio_streams=[],fps=24,width=1280,height=720)
    monkeypatch.setattr(cut,"_metadata",sample_metadata)
    shore=call("choose",choice="shore")["turn"]
    end=call("choose",shore,choice="beacon")["turn"]
    assert not call("view",end)["film_placements"]
    board=call("film",end)
    head,middle,final=board["shots"]
    def target_seconds(turn):
        dest=call("view",turn)["film_placements"][0]
        row=next(c for c in cut._view(p,p.state())["clips"] if c["shot"]==dest["shot"])
        assert row["take"]==dest["take"]
        return dest["shot"],row["start_frame"]/24
    def edit(op,**args):
        cut._mutate(p,op,dict(version=cut._version(p.state()),**args))
    assert target_seconds(end)==(final,17)
    edit("cut_trim",shot=middle,start=.5,end=2.5)
    trimmed=p.state()["shots"][middle]["selected_take"]
    assert target_seconds(end)==(final,16)
    edit("cut_remove",shot=middle)
    assert not call("view",shore)["film_placements"] and target_seconds(end)==(final,14)
    assert call("film",end)==board
    assert call("view",shore)["film_placements"][0]["take"]==trimmed
    assert target_seconds(end)==(final,16)
    edit("cut_undo")
    assert p.state()["cut"][-2:]==[head,final] and target_seconds(end)==(final,14)
    edit("cut_redo")
    assert target_seconds(end)==(final,16)
    other=call("choose",shore,choice="silence")["turn"]
    other_board=call("film",other)
    assert [c["shot"] for c in call("view",root)["film_placements"]]==[other_board["shots"][0],head]
    assert call("film",end)==board and target_seconds(root)==(head,10)


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),reason="Decoded frame needs media tools")
def test_saved_viewpoint_becomes_exact_first_frame_once(relay,tmp_path):
    from PIL import Image
    from h3studio import media
    p,root,call=relay
    shore=call("choose",choice="shore")["turn"]
    take=call("view",shore)["visual"]["take"]
    mark=call("bookmark",shore,take=take,seconds=.75)["waypoint"]
    shot_id=call("motion",shore,waypoint=mark["id"])["shot"]
    assert call("motion",shore,waypoint=mark["id"])=={"shot":shot_id}
    again=call("bookmark",shore,take=take,waypoint=mark["id"],seconds=mark["seek_seconds"])["waypoint"]
    assert again["motion_shot"]==shot_id and again["id"]==mark["id"]
    doc=Project(p.folder).state()
    shot=doc["shots"][shot_id]
    assert shot["recipe"]["mode"]=="i2va" and shot["game_turn"]==shore
    guide=shot["guides"][0]
    assert guide["frame"]==0 and guide["usage"]=="both"
    rev=doc["revisions"][guide["revision"]]
    assert doc["source_frames"][rev["composition"]]["index"]==18
    source=p.asset_path(doc["takes"][take]["video_asset"])
    expected=tmp_path/"expected.png"
    media.extract_frame(source,mark["source_pts"],expected)
    with Image.open(expected) as frame:
        assert frame.convert("RGB").tobytes()==p.render_revision(rev["id"]).convert("RGB").tobytes()
