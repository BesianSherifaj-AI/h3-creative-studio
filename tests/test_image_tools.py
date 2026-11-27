import hashlib
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from PIL import Image

from h3studio import image_tools as tools
from h3studio.jobs import Queue
from h3studio.project import Project
from vendor.image_graph.workflows import REGISTRY


def schema():
    def node(required, outputs, **kw):
        return dict(input={"required":{k:([v] if isinstance(v,str) else v) for k,v in required.items()}}, output=outputs, **kw)
    def choices(*values):
        return [list(values)]
    model = REGISTRY["h3"]
    precision = ["COMFY_DYNAMICCOMBO_V3",{"options":[{"key":"auto","inputs":{
        "required":{"level":["INT",{"default":2,"min":0,"max":4}]}}}]}]
    return {
        "CLIPLoader":node({"clip_name":choices(model["clip"]),"type":choices("minimax")},["CLIP"]),
        "VAELoader":node({"vae_name":choices(model["vae"])},["VAE"]),
        "UNETLoader":node({"unet_name":choices(model["unet"]),"weight_dtype":choices("default"),"precision":precision},["MODEL"]),
        "MiniMaxH3ImageConditioning":dict(input={
            "required":{"clip":["CLIP"],"prompt":["STRING"],"width":["INT"],"height":["INT"]},
            "optional":{"vae":["VAE"],"images":["COMFY_AUTOGROW_V3",{"template":{
                "prefix":"image_","min":0,"max":4,"input":{"required":{"image":["IMAGE"]}}}}]}},
            output=["CONDITIONING","LATENT"]),
        "LoadImage":node({"image":choices("before-upload.png")},["IMAGE","MASK"]),
        "ConditioningZeroOut":node({"conditioning":"CONDITIONING"},["CONDITIONING"]),
        "KSampler":node(dict(model="MODEL",positive="CONDITIONING",negative="CONDITIONING",
            latent_image="LATENT",seed="INT",steps="INT",cfg="FLOAT",sampler_name=choices("euler"),
            scheduler=choices("simple"),denoise="FLOAT"),["LATENT"]),
        "VAEDecode":node({"samples":"LATENT","vae":"VAE"},["IMAGE"]),
        "SaveImage":node({"images":"IMAGE","filename_prefix":"STRING"},[],output_node=True),
    }


@pytest.fixture
def case(tmp_path,monkeypatch):
    catalog,uploads=schema(),{}
    class Client:
        def __init__(self,**kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self,*args):
            pass
        def post(self,url,files,data):
            filename,content,_=files["image"]
            assert data["overwrite"]=="false"
            uploads[data["subfolder"],filename]=content
            return httpx.Response(200,request=httpx.Request("POST",url),
                json=dict(name=filename,subfolder=data["subfolder"],type="input"))
        def get(self,url,params=None):
            if url.endswith("object_info"):
                return httpx.Response(200,request=httpx.Request("GET",url),json=catalog)
            return httpx.Response(200,request=httpx.Request("GET",url),content=uploads[params["subfolder"],params["filename"]])
    monkeypatch.setattr(tools.httpx,"Client",Client)
    p = Project.create(tmp_path/"Film","Compositions")
    source = tmp_path/"Original.png"
    Image.new("RGBA",(64,48),(220,20,20,255)).save(source)
    rev = p.save_composition("Original",64,48,[{"asset":p.add_asset(source)}])
    studio = SimpleNamespace(prefs={"image_url":"http://127.0.0.1:8888"},queue=Queue(tmp_path/"spool",guard=lambda:[]))
    return studio,p,rev,uploads,catalog


def output(path, im):
    im.save(path)
    return dict(filename=path.name,path=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def test_mask_source_readback_verified_outputs_and_portability(case,tmp_path):
    studio,p,rev,uploads,catalog=case
    mask=Image.new("L",(64,48),0)
    mask.paste(255,(19,16,45,33))
    mask.save(tmp_path/"mask.png")
    request = dict(task="edit",model="h3",revision=rev,mask_path=str(tmp_path/"mask.png"),prompt="Cool the selected area.")
    job = tools.prepare_image(studio,p,request,p.state())
    pl = studio.queue.get(job["id"],full=True)["payload"]
    spec = pl["image_spec"]
    assert pl["prompt"]["1"]["inputs"]["precision.level"]==2
    assert pl["prompt"]["1"]["inputs"]["precision"]=="auto"
    assert pl["prompt"]["5"]["inputs"]["images.image_0"]==["10",0]
    assert spec["canvas"]==[64,48]
    assert Image.open(BytesIO(next(iter(uploads.values())))).size == (spec["width"],spec["height"])
    result = output(tmp_path/"result.png",Image.new("RGBA",(spec["width"],spec["height"]),(10,20,240,255)))
    with pytest.raises(ValueError,match="checksum"):
        tools.adopt_image(studio,p,pl,{"outputs":[dict(result,sha256="0"*64)]},job)
    adopted=tools.adopt_image(studio,p,pl,{"outputs":[result]},job)
    assert tools.adopt_image(studio,p,pl,{"outputs":[]},job) == adopted
    reopened = Project(p.folder)
    pixels=reopened.render_revision(adopted["revision"])
    assert pixels.size==(64,48) and pixels.getpixel((1,1))==(220,20,20,255) and pixels.getpixel((25,22))==(10,20,240,255)
    assert reopened.state()["revisions"][adopted["revision"]]["generation"]["source_revision"]==rev
    catalog["UNETLoader"]["input"]["required"]["unet_name"]=[["a different model"]]
    with pytest.raises(Exception,match="unet_name"):
        tools.prepare_image(studio,p,request,p.state())


def test_outpaint_keeps_source_and_rejects_wrong_dimensions(case,tmp_path):
    studio,p,rev,_,_=case
    job = tools.prepare_image(studio,p,dict(task="outpaint",model="h3",revision=rev,padding=32,prompt="Continue the pattern."),p.state())
    pl = studio.queue.get(job["id"],full=True)["payload"]
    spec = pl["image_spec"]
    assert spec["canvas"]==[128,112]
    with pytest.raises(ValueError,match="expected"):
        tools.adopt_image(studio,p,pl,{"outputs":[output(tmp_path/"bad.png",Image.new("RGBA",(16,16)))]},job)
    result=output(tmp_path/"out.png",Image.new("RGB",(spec["width"],spec["height"]),"blue"))
    adopted=tools.adopt_image(studio,p,pl,{"outputs":[result]},job)
    pixels=p.render_revision(adopted["revision"])
    assert pixels.getpixel((0,0))==(0,0,255,255) and pixels.getpixel((32,32))==(220,20,20,255)
    assert pixels.size==(128,112)


def test_qwen_layer_count_alpha_order_and_separate_reconstruction(case,tmp_path):
    studio,p,rev,_,_=case
    pl={"studio_revision":rev,"image_spec":{"task":"layers","model":"layered","layers":2,"width":32,"height":32,"canvas":[64,48]}}
    outputs=[output(tmp_path/f"batch-{i}.png",Image.new("RGBA",(32,32),color))
             for i,color in enumerate(("white","red",(0,0,255,128)))]
    job={"id":"layered-image"}
    with pytest.raises(ValueError,match="Expected 3"):
        tools.adopt_image(studio,p,pl,{"outputs":outputs[1:]},job)
    with pytest.raises(ValueError,match="alpha"):
        tools.adopt_image(studio,p,pl,{"outputs":[*outputs[:2],output(tmp_path/"rgb.png",Image.new("RGB",(32,32)))]},job)
    rev=tools.adopt_image(studio,p,pl,{"outputs":outputs},job)["revision"]
    state=p.state()["revisions"][rev]
    assert [layer["name"] for layer in state["layers"]] == ["Layer 1","Layer 2"]
    assert state["generation"]["reconstruction"] not in {layer["asset"] for layer in state["layers"]}
    assert p.render_revision(rev).getpixel((10,10))==(127,0,128,255)


def test_saved_qwen_preset_revalidates_exact_model(case):
    studio,p,_,_,catalog = case
    model = REGISTRY["qwen21"]
    def choices(node, key):
        return catalog[node]["input"]["required"][key][0]
    choices("UNETLoader","unet_name").append(model["unet"])
    choices("CLIPLoader","clip_name").append(model["clip"])
    choices("CLIPLoader","type").append("qwen_image")
    choices("VAELoader","vae_name").append(model["vae"])
    catalog["TextEncodeQwenImage21"] = {
        "input":{"required":{key:[kind] for key,kind in
                  dict(clip="CLIP",vae="VAE",prompt="STRING",negative_prompt="STRING",resolution="INT").items()}},
        "output":["CONDITIONING","CONDITIONING","LATENT"]}
    preset = dict(task="create",model="qwen21",prompt="Stained glass",seed=44,width=512,height=512)
    prepared = tools.prepare_image(studio,p,preset,p.state())
    job = studio.queue.get(prepared["id"],full=True)
    assert job["payload"]["prompt"]["1"]["inputs"]["unet_name"] == model["unet"]
    choices("UNETLoader","unet_name").remove(model["unet"])
    with pytest.raises(ValueError,match=model["unet"]):
        tools.prepare_image(studio,p,job["payload"]["image_spec"],p.state())
    assert len(studio.queue.list()) == 1


def test_masked_recolor_retains_original_luminance_texture_and_coverage():
    rng = np.random.default_rng(42)
    source = rng.integers(0,255,(32,40,4),dtype=np.uint8)
    before = Image.fromarray(source)
    edit = Image.new("RGBA",before.size,(0,45,250,255))
    mask = Image.new("L",before.size,0)
    mask.paste(255,(5,7,27,28))
    for task in ("recolor","remove"):
        result = np.asarray(tools._masked_result(before,edit,mask,task))
        selected = np.asarray(mask)>0
        assert np.array_equal(result[~selected],source[~selected])
        assert np.array_equal(result[:,:,3],source[:,:,3])
        if task=="recolor":
            drift = (result[:,:,:3].astype(float)-source[:,:,:3]) @ np.array([.299,.587,.114])
            assert abs(drift).max()<=.501
            assert np.count_nonzero(np.any(result[selected]!=source[selected],axis=1))>400


def test_shadow_correction_uses_local_surface_and_keeps_patterned_texture():
    y,x=np.mgrid[:128,:128]
    plane=np.stack([190+x//15,188+x//15,170+x//15],axis=2).astype(np.uint8)
    coverage=np.zeros((128,128),np.uint8)
    coverage[38:92,36:94]=255
    mask=Image.fromarray(coverage)
    src=plane.copy()
    src[54:75,54:74]=[205,200,4]
    prediction=plane.copy()
    prediction[42:85,42:88]-=16
    def rgba(arr):
        return Image.fromarray(arr).convert("RGBA")
    fixed=np.asarray(tools._masked_result(rgba(src),rgba(prediction),mask,"remove"))
    assert abs(fixed[55:74,54:74,:3].astype(float)-plane[55:74,54:74]).mean()<2
    assert np.array_equal(fixed[coverage==0,:3],plane[coverage==0])
    # A fine weave crosses the target from all sides. Keep this real frequency.
    texture=plane+((x*3+y*7)%33)[:,:,None].astype(np.uint8)
    assert tools._smooth_repair(texture,texture,coverage) is None
    textured_source=texture.copy()
    textured_source[54:75,54:74]=[205,200,4]
    result=np.asarray(tools._masked_result(rgba(textured_source),rgba(texture),mask,"remove"))
    assert np.corrcoef(result[55:74,54:74,0].ravel(),texture[55:74,54:74,0].ravel())[0,1]>.98


def test_small_texture_repair_continues_slanted_ribs_and_keeps_protected_pixels():
    y, x = np.mgrid[:123, :125]
    coverage = np.uint8(((x-64)/21)**2 + ((y-58)/18)**2 < 1) * 255
    target = coverage > 0
    grain = np.random.default_rng(485).normal(0, 1.5, x.shape)
    surface = 90 + 22*np.cos((x+y*.5)*.32) + grain
    clean = np.stack((surface+8, surface+4, surface), axis=2).astype(np.uint8)
    marked, proposal = clean.copy(), clean.copy()
    marked[target] = [248, 30, 100]
    # A sharp, misplaced diagonal hatch contradicts the existing soft ribs.
    hatch = np.where((x-y) % 9 < 4, -52., 52.)
    proposal[target] = np.clip(clean[target].astype(float) + hatch[target, None], 0, 255)
    mask = Image.fromarray(coverage)
    before = Image.fromarray(marked).convert("RGBA")
    prediction = Image.fromarray(proposal).convert("RGBA")
    kept = np.asarray(tools._masked_result(before, prediction, mask, "remove", "texture"))[:, :, :3]
    automatic = np.asarray(tools._masked_result(before, prediction, mask, "remove"))[:, :, :3]
    check = np.asarray(tools._masked_result(before, prediction, mask, "remove"))[:, :, :3]
    assert np.array_equal(automatic, check) and np.array_equal(automatic[~target], clean[~target])
    def error(result):
        return abs(result.astype(float) - clean)[target].mean()
    flat = np.broadcast_to(np.median(clean[~target], axis=0), clean.shape)
    assert error(automatic) < min(error(kept), error(flat)) * .65
    assert np.corrcoef(automatic[:, :, 0][target], clean[:, :, 0][target])[0, 1] > .85
