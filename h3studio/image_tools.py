"""Prepare Atelier image workflows and archive their editable, protected results."""
from __future__ import annotations

import copy
import hashlib
import io
import uuid
from pathlib import Path

import httpx
from PIL import Image, ImageOps

from vendor.image_graph import workflows
from vendor.prompt import comfy_transfer as graph_tools
from vendor.prompt.resources import local_url
from .project import _size


def _smooth_repair(original, edited, mask):
    """Match an evenly lit background only where surrounding samples support a smooth fit."""
    import cv2
    import numpy as np
    x, y, w, h = cv2.boundingRect(mask)
    if min(w,h) < 8:
        return None
    margin = max(8,min(80,round(min(w,h)*.28)))
    left, top = max(0,x-margin), max(0,y-margin)
    box = np.s_[top:y+h+margin,left:x+w+margin]
    area, target = original[box], mask[box]
    # A bounded perimeter fit rejects subject edges and nearby objects.
    outside = (target == 0).astype(np.uint8)
    distance = cv2.distanceTransform(outside,cv2.DIST_L2,3)
    yy, xx = np.nonzero((distance >= 2) & (distance <= margin))
    if len(xx)<80:
        return None
    step=max(1,len(xx)//4096)
    xx, yy = xx[::step], yy[::step]
    values=area[yy,xx].astype(float)
    def design(px, py):
        u=(px+left-x)/w-.5
        v=(py+top-y)/h-.5
        return np.stack((np.ones_like(u),u,v),axis=-1)
    basis=design(xx,yy)
    # A nearby object can contaminate a sizeable portion of the ring. Select
    # a plane supported by scattered background pixels before least squares.
    rng=np.random.default_rng(19)
    best=np.zeros(len(xx),dtype=bool)
    for _ in range(64):
        sample=rng.choice(len(xx),3,replace=False)
        if abs(np.linalg.det(basis[sample])) < .05:
            continue
        candidate=np.linalg.solve(basis[sample],values[sample])
        if np.max(np.abs(candidate[1:])) > 65:
            continue
        inside=abs(values-basis@candidate).max(axis=1)<=5
        if inside.sum()>best.sum():
            best=inside
    if best.mean()<.55:
        return None
    fit=np.linalg.lstsq(basis[best],values[best],rcond=None)[0]
    # Retain fine grain from the generated fill while replacing broad residual shadows.
    proposal=edited[box].astype(np.float32)
    grain=proposal-cv2.GaussianBlur(proposal,(0,0),2)
    repaired=edited.copy()
    for row in range(0,area.shape[0],96):
        py,px=np.mgrid[row:min(area.shape[0],row+96),:area.shape[1]]
        color=design(px,py)@fit+np.clip(grain[row:row+96],-5,5)
        repaired[box][row:row+96]=np.rint(color).clip(0,255).astype(np.uint8)
    return repaired


def _limit_texture(original, proposal, mask):
    """Continue nearby structure through a small hole if generated detail is an outlier."""
    import cv2
    import numpy as np
    x, y, w, h = cv2.boundingRect(mask)
    if min(w, h) < 12 or max(w, h) > 128 or not x or not y or x+w >= mask.shape[1] or y+h >= mask.shape[0]:
        return None
    pad = max(12, min(40, min(w, h) // 2))
    box = np.s_[max(0, y-pad):y+h+pad, max(0, x-pad):x+w+pad]
    src, candidate, hole = original[box], proposal[box], mask[box]
    inside = cv2.distanceTransform(hole, cv2.DIST_L2, 3) > 4
    outside = cv2.distanceTransform((hole == 0).astype(np.uint8), cv2.DIST_L2, 3)
    # Keep samples clear of the marked object's contribution to either filter.
    ring = (outside >= 6) & (outside < pad)
    if min(inside.sum(), ring.sum()) < 64:
        return None

    def detail(rgb):
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
        smooth = cv2.GaussianBlur(gray, (0, 0), 1.5)
        dx = cv2.Sobel(smooth, cv2.CV_32F, 1, 0, scale=.125)
        dy = cv2.Sobel(smooth, cv2.CV_32F, 0, 1, scale=.125)
        return gray-smooth, dx, dy

    src_grain, dx, dy = detail(src)
    gen_grain, gx, gy = detail(candidate)
    ref = np.percentile(abs(src_grain[ring]), 90)
    gen = np.percentile(abs(gen_grain[inside]), 90)
    ref_edge = np.percentile(np.hypot(dx[ring], dy[ring]), 90)
    gen_edge = np.percentile(np.hypot(gx[inside], gy[inside]), 90)
    if gen <= 2*ref + 2 or gen_edge <= 2*ref_edge + 2:
        return None

    base = cv2.inpaint(src, hole, 3, cv2.INPAINT_NS).astype(np.float32)
    gradients = np.stack((dx[ring], dy[ring]), axis=1)
    energy, axes = np.linalg.eigh(gradients.T @ gradients)
    coherence = (energy[1] - energy[0]) / max(float(energy.sum()), 1e-6)
    # A coherent structure tensor gives the direction in which a fold or stripe
    # continues. Interpolate between unmasked boundaries along this direction.
    py, px = np.nonzero(hole)
    if coherence > .3:
        tx, ty = axes[:, 0]
        sides = []
        for sign in (-1, 1):
            distances = np.zeros(len(px), np.float32)
            colors = base[py, px].copy()
            for step in range(1, int(np.hypot(w, h)) + 2):
                sx = np.rint(px + sign*step*tx).astype(int)
                sy = np.rint(py + sign*step*ty).astype(int)
                valid = (sx >= 0) & (sx < hole.shape[1]) & (sy >= 0) & (sy < hole.shape[0])
                sx = sx.clip(0, hole.shape[1]-1)
                sy = sy.clip(0, hole.shape[0]-1)
                hit = valid & (distances == 0) & (hole[sy, sx] == 0)
                colors[hit] = src[sy[hit], sx[hit]]
                distances[hit] = step
                if distances.all():
                    break
            sides.append((colors, distances))
        (a, da), (b, db) = sides
        both = (da > 0) & (db > 0)
        weight = da[both, None] / (da[both, None] + db[both, None])
        directional = a[both]*(1-weight) + b[both]*weight
        mix = min(1., (coherence-.3)/.5)
        base[py[both], px[both]] = (1-mix)*base[py[both], px[both]] + mix*directional
    # The model can still supply restrained fine grain. Bound its amplitude by
    # the observed background so a hallucinated crease cannot dominate the fill.
    grain = np.clip(gen_grain * (ref / max(gen, 1.)), -ref, ref)
    base_gray = cv2.cvtColor(base, cv2.COLOR_RGB2GRAY)
    base_detail = (base_gray - cv2.GaussianBlur(base_gray, (0, 0), 1.5))[inside]
    missing_energy = max(0., np.mean(src_grain[ring]**2) - np.mean(base_detail**2))
    gain = min(1., np.sqrt(missing_energy / max(.01, np.mean(grain[inside]**2))))
    patch = np.rint(base + gain*grain[..., None]).clip(0, 255).astype(np.uint8)
    result = proposal.copy()
    result[box][py, px] = patch[py, px]
    return result


def _masked_result(original, edited, mask, task, surface="auto"):
    if task not in ("remove","recolor"):
        return Image.composite(edited,original,mask)
    import cv2
    import numpy as np
    original_rgb = np.asarray(original.convert("RGB"))
    edited_rgb = np.asarray(edited.convert("RGB"))
    if task == "remove":
        # Fit the proposed repair to the surrounding light before applying the exact selection.
        region = np.where(np.asarray(mask)>0,255,0).astype(np.uint8)
        matched = _smooth_repair(original_rgb,edited_rgb,region) if surface == "auto" else None
        region[[0,-1],:] = 0
        region[:,[0,-1]] = 0
        x,y,w,h = cv2.boundingRect(region)
        if min(w,h) < 4:
            return Image.composite(edited,original,mask)
        color = cv2.seamlessClone(edited_rgb,original_rgb,region,(x+w//2,y+h//2),cv2.NORMAL_CLONE)
        if surface == "auto" and matched is None:
            bounded = _limit_texture(original_rgb, edited_rgb, np.asarray(mask))
            if bounded is not None:
                color = bounded
        if matched is not None:
            # Keep the natural contact shadow at the edge of the editable region.
            inside=(np.asarray(mask)>0).astype(np.uint8)
            feather=np.clip(cv2.distanceTransform(inside,cv2.DIST_L2,3)/max(4,min(36,min(w,h)*.22)),0,1)[...,None]
            weight=feather*feather*(3-2*feather)
            color=np.rint(color*(1-weight)+matched*weight).astype(np.uint8)
    else:
        # Retain each source pixel's luminance, including leather grain, seams and reflections.
        weights = np.array([.299,.587,.114],dtype=np.float32)
        luminance = (original_rgb @ weights)[...,None]
        chroma = edited_rgb.astype(np.float32) - (edited_rgb @ weights)[...,None]
        low = np.maximum(-chroma.min(axis=2,keepdims=True),1e-6)
        high = np.maximum(chroma.max(axis=2,keepdims=True),1e-6)
        gain = np.minimum(1,np.minimum(luminance/low,(255-luminance)/high))
        color = np.rint(luminance+gain*chroma).clip(0,255).astype(np.uint8)
    adjusted = Image.fromarray(color)
    adjusted.putalpha(original.getchannel("A"))
    return Image.composite(adjusted,original,mask)


def _int(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be a whole number from {minimum} to {maximum}.")
    return value


def _png(image):
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _read(path, mode):
    with Image.open(path) as image:
        _size(*image.size)
        return ImageOps.exif_transpose(image).convert(mode)


def _store(project, data, name, sha=None):
    with io.BytesIO(data) as stream:
        record = project.store_stream(stream, name, expected_hash=sha)
    with project.transaction() as state:
        state["assets"].setdefault(record["id"], record)
    return record["id"]


def _upload(client, base, folder, name, image):
    raw = _png(image)
    dest = {"name":name, "type":"input", "subfolder":folder}
    response = client.post(base+"/upload/image",files={"image":(name,raw,"image/png")},
                           data={"subfolder":folder,"type":"input","overwrite":"false"})
    response.raise_for_status()
    if any(response.json().get(k) != v for k,v in dest.items()):
        raise ValueError("The image renderer returned a different upload destination.")
    response = client.get(base+"/view", params={"filename":name,"type":"input","subfolder":folder})
    response.raise_for_status()
    if hashlib.sha256(response.content).digest() != hashlib.sha256(raw).digest():
        raise ValueError("The renderer's image readback differs from the uploaded image.")
    return folder+"/"+name


def _image_defaults(graph, schema, names):
    """Expand native widget defaults, including nested V3 dynamic combo branches."""
    schema = copy.deepcopy(schema)
    for node in graph.values():
        inputs = node["inputs"]
        definition = schema.get(node["class_type"])
        if not definition:
            continue
        # LoadImageMask's enumerated files can predate the upload; only accept our readback.
        if node["class_type"] == "LoadImageMask" and inputs["image"] in names:
            choices = graph_tools._choices(definition["input"]["required"]["image"])
            if inputs["image"] not in choices:
                choices.append(inputs["image"])
        for _ in range(8):
            added = False
            for key,(field,required) in graph_tools._fields(definition,inputs).items():
                if key in inputs or not graph_tools._widget(field):
                    continue
                opts = field[1] if len(field)>1 and isinstance(field[1],dict) else {}
                # Models are specified by exact filename in the submitted graph.
                # A newly required loader input must get an explicit value from the recipe.
                if required and ("model" in key or key.endswith("_name")):
                    continue
                dynamic = graph_tools._type(field) == "COMFY_DYNAMICCOMBO_V3"
                if required and not dynamic and "default" not in opts:
                    continue
                default = graph_tools._default(field)
                if default is not None:
                    inputs[key] = default
                    added = True
            if not added:
                break
    graph_tools._validate_graph(graph,schema,names)


def _inputs(project, spec, revision, args):
    if revision is None:
        if spec["task"] != "create":
            raise ValueError("Select a saved composition revision to edit.")
        return None, None
    original = project.render_revision(revision["id"])
    mask = _read(args["mask_path"],"L") if args.get("mask_path") else None
    if mask and mask.size != original.size:
        raise ValueError("The edit mask must match the source canvas dimensions.")
    if mask and spec["task"] not in ("edit","remove","recolor","cutout"):
        raise ValueError("Choose edit, remove, recolor or cutout to use an edit mask.")
    if mask and not mask.getbbox():
        raise ValueError("The edit mask is entirely protected. Paint the editable area white.")
    if spec["task"] == "outpaint":
        padding = _int(args.get("padding",128),32,512,"Outpaint padding")
        spec["padding"] = padding
        dimensions = original.width + padding*2, original.height + padding*2
        _size(*dimensions)
        expanded = Image.new("RGBA",dimensions,(240,240,240,255))
        expanded.paste(original,(padding,padding))
        mask = Image.new("L",dimensions,255)
        mask.paste(0,(padding,padding,padding+original.width,padding+original.height))
        original = expanded
    spec["source_revision"] = revision["id"]
    spec["original"] = _store(project,_png(original),"Protected canvas.png")
    spec["canvas"] = list(original.size)
    if mask:
        spec["mask"] = _store(project,_png(mask),"Edit mask.png")
    # Render within Atelier's working size and compose at the original resolution.
    limit = 640 if spec["model"] == "layered" else 2048
    factor = min(1,limit/max(original.size))
    if spec["model"] == "h3" and max(original.size) < 1024:
        factor = 1024/max(original.size)
    spec["width"], spec["height"] = [max(32,round(side*factor/32)*32) for side in original.size]
    return original.resize((spec["width"],spec["height"]),Image.Resampling.LANCZOS), mask


def prepare_image(studio, p, a, doc):
    model, task = a["model"], a["task"]
    if model not in workflows.REGISTRY or task not in workflows.REGISTRY[model]["tasks"]:
        raise ValueError("The selected model does not implement this image operation.")
    prompt = a.get("prompt","")
    if not isinstance(prompt,str) or not prompt.strip() or len(prompt)>6000:
        raise ValueError("Enter an image prompt of up to 6000 characters.")
    spec = dict(model=model,task=task,prompt=prompt,seed=_int(a.get("seed",0),0,2**53-1,"Seed"))
    if task == "remove":
        spec["surface"] = a.get("surface","auto")
        if spec["surface"] not in ("auto","texture"):
            raise ValueError("Choose automatic surface matching or generated texture.")
    if task == "create":
        for key in ("width","height"):
            value = _int(a.get(key,768),256,2048,key.title())
            if value % 32:
                raise ValueError("Image dimensions must be multiples of 32.")
            spec[key] = value
        if a.get("mask_path"):
            raise ValueError("Select an edit operation to use a mask.")
    if task == "layers":
        spec["layers"] = _int(a.get("layers",2),2,6,"Layer count")
    if task == "control":
        spec["control_mode"] = a.get("control_mode","canny")
        if spec["control_mode"] not in ("canny","gray","provided"):
            raise ValueError("Choose canny, gray or provided control.")
    references = a.get("reference_paths",[])
    if not isinstance(references,list) or len(references)>3 or references and model not in ("h3","qwen21"):
        raise ValueError("Choose up to three references for H3 Image or Qwen Image 2.1.")
    revision = doc["revisions"].get(a.get("revision")) if task!="create" else None
    original, mask = _inputs(p,spec,revision,a)
    ident = str(uuid.uuid4())
    folder = "h3creative/"+ident
    base = local_url(studio.prefs["image_url"])
    with httpx.Client(trust_env=False,timeout=60) as client:
        response = client.get(base+"/object_info")
        response.raise_for_status()
        schema = response.json()
        source = _upload(client,base,folder,"source.png",original) if original else None
        mask_name = control = None
        if mask is not None and model == "zimage":
            small = mask.resize(original.size,Image.Resampling.NEAREST)
            mask_name = _upload(client,base,folder,"mask.png",small)
        if task == "control":
            image = original
            if spec["control_mode"] == "canny":
                import cv2
                import numpy as np
                image = Image.fromarray(cv2.Canny(np.asarray(original.convert("RGB")),90,180))
            elif spec["control_mode"] == "gray":
                image = original.convert("L")
            control = _upload(client,base,folder,"control.png",image)
        refs = []
        for i,path in enumerate(references):
            image = _read(path,"RGBA")
            asset = _store(p,_png(image),Path(path).stem+".png")
            spec.setdefault("references",[]).append(asset)
            image.thumbnail((1536,1536),Image.Resampling.LANCZOS)
            refs.append(_upload(client,base,folder,f"reference-{i}.png",image))
        graph = workflows.build(spec,ident,source,mask_name,control,refs)
        _image_defaults(graph,schema,{name for name in (source,mask_name,control,*refs) if name})
    payload = dict(comfy_url=base,prompt=graph,studio_project=doc["id"],image_spec=spec,
                   studio_revision=revision["id"] if revision else None)
    return studio.queue.enqueue("image",payload,ident)


def adopt_image(studio, p, pl, receipt, job):
    if previous := p.state().get("image_jobs",{}).get(job["id"]):
        return {"revision":previous}
    outputs = receipt.get("outputs") or []
    spec = pl.get("image_spec",{})
    layered = spec.get("task") == "layers"
    expected = spec.get("layers",2)+1 if layered else 1
    if not outputs or (spec and len(outputs)!=expected):
        raise ValueError(f"Expected {expected} saved image output(s) for this request.")
    size = (spec["width"],spec["height"]) if spec else None
    canvas = tuple(spec.get("canvas",size or []))
    layers, raw = [], []
    generation = {"job_id":job["id"],"source_revision":pl.get("studio_revision"),"settings":spec,
                  "worker":receipt.get("worker"),"devices":receipt.get("devices"),
                  "original":spec.get("original"),"mask":spec.get("mask"),"raw_outputs":raw,"graph":pl.get("prompt")}
    for i,item in enumerate(outputs):
        if not item.get("sha256"):
            raise ValueError("The renderer did not supply an image checksum.")
        with Path(item["path"]).open("rb") as handle:
            record = p.store_stream(handle,item.get("filename") or "Output.png",expected_hash=item["sha256"])
        with p.transaction() as state:
            state["assets"].setdefault(record["id"],record)
        path = p.asset_path(record["id"])
        with Image.open(path) as output:
            _size(*output.size)
            if size is None:
                size = output.size
                canvas = size
            if output.size != size:
                raise ValueError(f"Image output {i+1} is {output.size}, expected {size}.")
            if layered and i and "A" not in output.getbands() and "transparency" not in output.info:
                raise ValueError("Qwen decomposition returned a layer without alpha.")
            rendered = output.convert("RGBA")
        raw.append(record["id"])
        if rendered.size != canvas:
            rendered = rendered.resize(canvas,Image.Resampling.LANCZOS)
        if layered and i == 0:
            generation["reconstruction"] = _store(p,_png(rendered),"Reconstruction.png")
            continue
        if generation["mask"]:
            mask = _read(p.asset_path(generation["mask"]),"L")
            original = _read(p.asset_path(generation["original"]),"RGBA")
            if mask.size != canvas or original.size != canvas:
                raise ValueError("Saved mask or original differs from the requested canvas.")
            rendered = _masked_result(original,rendered,mask,spec.get("task"),spec.get("surface","auto"))
        label = f"Layer {i}" if layered else item.get("filename","Image")
        asset = _store(p,_png(rendered),label+".png")
        layers.append(dict(name=label,asset=asset))
    name = "Qwen image layers" if layered else "AI "+spec.get("task","image")
    revision = p.save_composition(name,*canvas,layers,job_id=job["id"],generation=generation)
    return {"revision":revision}
