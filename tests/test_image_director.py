import hashlib
import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from h3studio import image_director as vd
from h3studio.image_room import action
from h3studio.image_tools import prepare_helper
from h3studio.project import Project
from test_image_tools import case


def vision_catalog(catalog):
    catalog["CLIPLoader"]["input"]["required"]["clip_name"][0].append(vd.VISION_MODELS[0])
    catalog["CLIPLoader"]["input"]["required"]["type"][0].append("stable_diffusion")
    catalog.update({
        "TextGenerateV2":{"input":{"required":{"clip":["CLIP"],"prompt":["STRING"],"max_length":["INT"],
            "thinking":["BOOLEAN"],"sampling_mode":[["off"]],"use_default_template":["BOOLEAN"]},"optional":{
            "images":["COMFY_AUTOGROW_V3",{"template":{"names":["image1","image2"],"min":0,"input":{"required":{"image":["IMAGE"]}}}}]}},
            "output":["STRING","STRING"]},
        "SaveText":{"input":{"required":{"text":["STRING",{"forceInput":True}],"format":[["txt"]],"filename_prefix":["STRING"]}},
                    "output":["STRING"],"output_node":True}})
    return catalog


def args(rev,**extra):
    return {"revision":rev,"source":rev,"model":vd.VISION_MODELS[0],"phase":"plan","goal":"Cool the background.","approved":True,**extra}


def test_visual_plan_approval_exact_model_and_source_pair(case,tmp_path):
    studio,project,rev,uploads,catalog=case
    with pytest.raises(ValueError,match="Approve"):
        action(studio,project,"vision",args(rev,approved=False))
    with pytest.raises(ValueError,match="lacks vision support"):
        action(studio,project,"vision",args(rev))
    vision_catalog(catalog)
    with pytest.raises(ValueError,match="unavailable"):
        vd.prepare(studio,project,args(rev,model=vd.VISION_MODELS[1]))
    assert not studio.queue.list()
    selected=action(studio,project,"selection",dict(revision=rev,shape="rectangle",points=[[2,1],[8,4]]))["selection"]
    jid=vd.prepare(studio,project,args(rev))["id"];job=studio.queue.get(jid,full=True)
    graph=job["payload"]["prompt"]
    assert graph["1"]["inputs"]["clip_name"]==vd.VISION_MODELS[0] and graph["2"]["inputs"]["images.image2"]==["12",0]
    assert job["payload"]["visual_director"]["selection"]==selected["asset"]
    folder="h3creative/"+jid
    with Image.open(BytesIO(uploads[folder,"view-2.png"])) as overlay:
        assert overlay.getpixel((3,2)) != overlay.getpixel((20,20))
    png=tmp_path/"edited.png";Image.new("RGB",(64,48),"blue").save(png)
    result=project.save_composition("Cool background",64,48,[{"asset":project.add_asset(png)}])
    pair=vd.prepare(studio,project,args(result,phase="review",source=rev))["id"]
    with Image.open(BytesIO(uploads["h3creative/"+pair,"view-2.png"])) as edited:
        assert edited.getpixel((10,10))==(0,0,255)
    with pytest.raises(ValueError,match="different saved"):
        vd.prepare(studio,project,args(rev,phase="review"))


def test_archived_report_and_edit_lineage(case,tmp_path):
    studio,p,rev,uploads,catalog=case
    vision_catalog(catalog)
    jid=vd.prepare(studio,p,args(rev))["id"]
    job=studio.queue.get(jid,full=True)
    answer=dict(finding="The scene is red.",task="recolor",prompt="Cool the tones.",object="sky")
    path=tmp_path/"report.txt";path.write_text(json.dumps(answer))
    output=dict(node_id="3",path=str(path),sha256="bad")
    job["receipt"]={"outputs":[output]}
    with pytest.raises(ValueError,match="checksum"):
        vd.adopt(p,job)
    output["sha256"]=hashlib.sha256(path.read_bytes()).hexdigest()
    assert vd.adopt(p,job)==vd.adopt(p,job)=={"visual_note":jid}
    assert Project(p.folder).state()["visual_notes"][jid]["answer"]==answer
    edit=action(studio,p,"ai",dict(revision=rev,visual_note=jid,model="h3",task="recolor",prompt="Cool by 20%.",seed=2))
    spec=studio.queue.get(edit["id"],full=True)["payload"]["image_spec"]
    assert spec["visual_note"]==jid and spec["prompt"]=="Cool by 20%."
    missing=dict(job,id="unreadable")
    path.write_text("The sky is slightly cooler.");output["sha256"]=hashlib.sha256(path.read_bytes()).hexdigest()
    vd.adopt(p,missing)
    bad=p.state()["visual_notes"]["unreadable"]
    assert bad["raw"] and "answer" not in bad and bad["error"]
    with pytest.raises(ValueError,match="verdict"):
        vd.parse('{"finding":"Looks red","prompt":"","verdict":"revise"}',"review")


def test_sam_clicks_source_pixels_and_connected_string_inputs(case):
    studio,p,rev,uploads,c=case
    s=dict(revision=rev,model="sam",task="select",positive_points=[dict(x=23,y=27)],negative_points=[dict(x=40,y=10)])
    with pytest.raises(ValueError,match="inside"):
        prepare_helper(studio,p,{**s,"positive_points":[dict(x=64,y=10)]},p.state())
    c.update({
        "PrimitiveString":{"input":{"required":{"value":["STRING"]}},"output":["STRING"]},
        "CheckpointLoaderSimple":{"input":{"required":{"ckpt_name":[["sam3.1_multiplex_fp16.safetensors"]]}},"output":["MODEL","CLIP"]},
        "SAM3_Detect":{"input":{"required":{"model":["MODEL"],"image":["IMAGE"],"individual_masks":["BOOLEAN"],"threshold":["FLOAT"],"refine_iterations":["INT"]},
            "optional":{"positive_coords":["STRING",{"forceInput":True}],"negative_coords":["STRING",{"forceInput":True}]}},"output":["MASK"]},
        "MaskToImage":{"input":{"required":{"mask":["MASK"]}},"output":["IMAGE"]}})
    ident=prepare_helper(studio,p,s,p.state())["id"]
    g=studio.queue.get(ident,full=True)["payload"]["prompt"]
    assert g["4"]["inputs"]["positive_coords"]==["7",0] and g["4"]["inputs"]["negative_coords"]==["8",0]
    assert json.loads(g["7"]["inputs"]["value"])==s["positive_points"] and "CLIPTextEncode" not in str(g)
