from pathlib import Path
from types import SimpleNamespace
from threading import RLock

import pytest
from PIL import Image

from h3studio.application import Studio
from h3studio.project import Project
from test_image_tools import case  # worker catalog and upload/readback fixture


@pytest.fixture
def canvas(tmp_path):
    project = Project.create(tmp_path/"project","Night studies")
    def asset(name, size, color, mode="RGBA"):
        path=tmp_path/name
        Image.new(mode,size,color).save(path)
        return project.add_asset(path)
    layers=[dict(name="Sky",asset=asset("sky.png",(32,22),(7,30,80,255))),
            dict(name="Light",asset=asset("light.png",(6,8),(230,50,8,170)),x=8,y=5,rotation=15,
                 mask=asset("mask.png",(6,8),230,"L"))]
    revision=project.save_composition("Lamp",32,22,layers)
    studio=SimpleNamespace(lock=RLock(),project=project)
    def call(op,rev=revision,**args):
        return Studio.action(studio,"image_room_"+op,{"revision":rev,**args})
    return project,revision,call


def test_pixel_selection_combine_clear_and_restore(canvas):
    p,rev,call=canvas
    first=call("selection",shape="rectangle",points=[[15,13],[4,2]])["selection"]
    assert first["bounds"]==[4,2,16,14]
    with Image.open(p.asset_path(first["asset"])) as mask:
        assert mask.size==(32,22) and mask.getextrema()==(0,255)
        assert sum(n==255 for n in mask.get_flattened_data())==12*12
    next_mask=call("selection",shape="brush",points=[[25,15]],radius=2,combine="add",previous=first["asset"])["selection"]
    assert next_mask["bounds"]==[4,2,28,18]
    sub=call("selection",shape="lasso",points=[[0,0],[20,0],[0,19]],combine="subtract",previous=next_mask["asset"])["selection"]
    with Image.open(p.asset_path(sub["asset"])) as mask:
        assert mask.getpixel((4,2))==0 and mask.getpixel((25,15))==255
    assert Project(p.folder).state()["image_selections"][rev]==sub
    with pytest.raises(ValueError,match="selection changed"):
        call("clear",previous=first["asset"])
    with pytest.raises(ValueError,match="source canvas"):
        call("selection",shape="rectangle",points=[[0,0],[32,22]],previous=sub["asset"])
    call("clear",previous=sub["asset"])
    assert rev not in p.state()["image_selections"]


def test_color_wand_uses_only_connected_matching_pixels(canvas):
    p,rev,call=canvas
    # Crop a selection of identical sky on one side of a full-height contrasting line.
    path=p.folder/"split.png"
    image=Image.new("RGBA",(32,22),(9,10,7,255))
    image.paste((89,18,4,255),(14,0,15,22))
    image.paste((12,10,7,255),(4,4,8,8))
    image.save(path)
    rev=p.save_composition("Split",32,22,[dict(asset=p.add_asset(path))])
    result=call("selection",rev,shape="wand",points=[[3,3]],tolerance=4)["selection"]
    with Image.open(p.asset_path(result["asset"])) as mask:
        assert mask.getbbox()==(0,0,14,22) and mask.getpixel((5,5))==255 and mask.getpixel((20,5))==0


def test_adjust_transform_selection_cutout_resize_and_lossless_export(canvas,tmp_path):
    p,rev,call=canvas
    original=p.render_revision(rev)
    selection=call("selection",shape="lasso",points=[[3,2],[21,2],[3,18]])["selection"]["asset"]
    cutout=call("cutout",selection=selection)["revision"]
    result=p.render_revision(cutout)
    assert result.size==(19,17) and result.getpixel((18,16))[3]==0
    assert original.getpixel((3,2))==result.getpixel((0,0))
    resized=call("resize",width=21,height=10)["revision"]
    assert p.render_revision(resized).size==(21,10)
    assert p.state()["revisions"][resized]["composition"]!=p.state()["revisions"][rev]["composition"]
    with pytest.raises(ValueError,match="Canvas"):
        call("resize",width=8193,height=10)
    cropped=call("crop",selection=selection)["revision"]
    layers=p.state()["revisions"][cropped]["layers"]
    assert len(layers)==2 and layers[1]["x"]==5 and layers[1]["y"]==3 and layers[1]["rotation"]==15
    assert p.render_revision(cropped).tobytes()==original.crop((3,2,22,19)).tobytes()
    with pytest.raises(ValueError,match="canvas changed"):
        call("duplicate",index=1)
    adjusted=call("adjust",cropped,index=1,brightness=.8,contrast=1.1,saturation=.7,sharpness=1.2)["revision"]
    adjusted_layers=p.state()["revisions"][adjusted]["layers"]
    with Image.open(p.asset_path(adjusted_layers[1]["asset"])) as im:
        assert im.getpixel((0,0))[3]==170 and im.getpixel((0,0))[:3]!=(230,50,8)
    assert adjusted_layers[1]["mask"]==layers[1]["mask"]
    flipped=call("flip",adjusted,index=1,axis="x")["revision"]
    duplicate=call("duplicate",flipped,index=1)["revision"]
    removed=call("delete",duplicate,index=2)["revision"]
    assert p.state()["revisions"][removed]["layers"]==p.state()["revisions"][flipped]["layers"]
    for suffix in ("png","webp","jpg"):
        out=tmp_path/("image."+suffix)
        assert call("export",rev,path=str(out))=={"path":str(out)}
        with Image.open(out) as im:
            assert im.size==original.size
            if suffix!="jpg":
                assert im.convert("RGBA").tobytes()==original.tobytes()
        saved=out.read_bytes()
        with pytest.raises(ValueError,match="new image filename"):
            call("export",rev,path=str(out))
        assert out.read_bytes()==saved
    with pytest.raises(ValueError,match="outside project"):
        call("export",rev,path=str(p.folder/"assets"/"out.png"))


def test_first_middle_last_transfer_pins_source_and_preserves_audio(canvas):
    p,rev,call=canvas
    shot=call("guide",slot="first")["shot"]
    audio=p.state()["revisions"][rev]["layers"][0]["asset"]
    with p.transaction() as doc:
        doc["shots"][shot]["guides"].append(dict(role="audio",frame=60,asset=audio))
    call("guide",shot=shot,slot="middle")
    call("guide",shot=shot,slot="last")
    new=call("duplicate",index=1)["revision"]
    call("guide",new,shot=shot,slot="middle")
    saved=Project(p.folder).state()["shots"][shot]
    assert [(g["frame"],g["revision"]) for g in saved["guides"] if g["role"]=="image"]==[(0,rev),(60,new),(119,rev)]
    assert next(g for g in saved["guides"] if g["role"]=="audio")["asset"]==audio


def test_ai_uses_source_pixel_mask_and_full_alpha_cutout(canvas,monkeypatch):
    from h3studio import image_tools
    p,rev,call=canvas
    def capture(studio,project,fields,doc):
        return fields
    monkeypatch.setattr(image_tools,"prepare_image",capture)
    with pytest.raises(ValueError,match="selection"):
        call("ai",model="h3",task="remove",prompt="Remove the mark.")
    asset=call("selection",shape="rectangle",points=[[6,4],[15,16]])["selection"]["asset"]
    req=call("ai",model="h3",task="remove",selection=asset,seed=17,prompt="Remove the mark.",surface="texture")
    assert req["revision"]==rev and req["surface"]=="texture" and req["seed"]==17
    assert Path(req["mask_path"])==p.asset_path(asset)
    with pytest.raises(ValueError,match="selection"):
        call("ai",model="h3",task="edit",selection="old")
    assert "mask_path" not in call("ai",model="qwen21",task="cutout",selection=asset)


def test_seedvr_scales_connected_rgba_with_the_concrete_image_socket():
    from vendor.image_graph.helpers import build
    graph=build({"model":"seedvr2","scale":3},"source.png","probe")
    assert graph["5"]=={"class_type":"ImageScaleBy","inputs":{"image":["4",0],"scale_by":3,"upscale_method":"lanczos"}}
    assert graph["6"]["inputs"]["resized_images"]==["5",0]
    assert graph["11"]["inputs"]["original_resized_images"]==["5",0]


def test_desktop_removal_entry_enqueues_the_reviewed_workflow(case):
    studio,project,rev,uploads,catalog=case
    studio.project,studio.lock=project,RLock()
    asset=Studio.action(studio,"image_room_selection",dict(revision=rev,shape="rectangle",points=[[5,8],[23,29]]))["selection"]["asset"]
    job=Studio.action(studio,"image_room_ai",dict(revision=rev,selection=asset,model="h3",task="remove",
                                                    prompt="Remove the selected bright sticker.",surface="auto",seed=23))
    payload=studio.queue.get(job["id"],full=True)["payload"]
    assert payload["studio_revision"]==rev and payload["image_spec"]["mask"]==asset
    assert payload["image_spec"]["seed"]==23 and payload["image_spec"]["surface"]=="auto"
    assert "KSampler" in {node["class_type"] for node in payload["prompt"].values()} and uploads
