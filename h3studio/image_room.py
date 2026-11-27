"""Source-pixel selections and still authoring actions used by the Image workspace."""
import math
import os
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageOps


def _selection(project, revision, args):
    size = revision["width"], revision["height"]
    shape = args["shape"]
    points = args.get("points", [])
    if shape not in ("rectangle", "lasso", "brush", "wand") or not isinstance(points, list) or not 1 <= len(points) <= 16384:
        raise ValueError("Choose a rectangle, lasso, brush or color-wand selection.")
    for point in points:
        if (not isinstance(point, list) or len(point) != 2 or
                any(type(n) is not int or not 0 <= n < size[i] for i, n in enumerate(point))):
            raise ValueError("Selection coordinates must lie on the source canvas.")
    points = list(map(tuple, points))
    mask = Image.new("L", size)
    draw = ImageDraw.Draw(mask)
    if shape == "wand":
        import cv2
        import numpy as np
        tolerance = args.get("tolerance",24)
        if len(points) != 1 or type(tolerance) is not int or not 0 <= tolerance <= 255:
            raise ValueError("Click one source pixel with a color tolerance of 0–255.")
        pixels = np.asarray(project.render_revision(revision["id"]))
        x,y = points[0]
        seed = tuple(map(int,pixels[y,x]))
        within = cv2.inRange(pixels, tuple(max(0,v-tolerance) for v in seed), tuple(min(255,v+tolerance) for v in seed))
        cv2.floodFill(within,None,(x,y),127,flags=4)
        mask = Image.fromarray(cv2.compare(within,127,cv2.CMP_EQ))
    elif shape == "rectangle":
        if len(points) != 2:
            raise ValueError("Drag between opposite corners.")
        (x, y), (x2, y2) = points
        draw.rectangle((min(x,x2), min(y,y2), max(x,x2), max(y,y2)), fill=255)
    elif shape == "lasso":
        if len(points) < 3:
            raise ValueError("Draw a closed area with the lasso.")
        draw.polygon(points, fill=255)
    else:
        radius = args.get("radius")
        if type(radius) is not int or not 1 <= radius <= 512:
            raise ValueError("Choose a brush radius of 1–512 source pixels.")
        draw.line(points, fill=255, width=2*radius+1, joint="curve")
        for x,y in points:
            draw.ellipse((x-radius,y-radius,x+radius,y+radius), fill=255)
    combine = args.get("combine", "replace")
    if combine not in ("replace", "add", "subtract"):
        raise ValueError("Choose a selection operation.")
    with project.transaction() as doc:
        if doc["compositions"][revision["composition"]]["current"] != revision["id"]:
            raise ValueError("This canvas changed. Reopen the current revision.")
        selections = doc.setdefault("image_selections", {})
        previous = selections.get(revision["id"])
        if args.get("previous") != (previous or {}).get("asset"):
            raise ValueError("The selection changed. Reload the canvas before drawing again.")
        if combine != "replace":
            if previous:
                with Image.open(project._asset_path(doc, previous["asset"])) as handle:
                    prior = handle.convert("L")
            else:
                prior = Image.new("L", size)
            mask = ImageChops.lighter(prior, mask) if combine == "add" else ImageChops.subtract(prior, mask)
        bounds = mask.getbbox()
        if bounds:
            with TemporaryDirectory() as folder:
                path = Path(folder) / "Selection.png"
                mask.save(path)
                with path.open("rb") as stream:
                    record = project.store_stream(stream, path.name)
            doc["assets"].setdefault(record["id"], record)
            selections[revision["id"]] = {"asset":record["id"], "bounds":list(bounds)}
        else:
            selections.pop(revision["id"], None)
        return selections.get(revision["id"])


def _save_pixels(project, image, name):
    with TemporaryDirectory() as folder:
        path = Path(folder) / name
        image.save(path, format="PNG")
        return project.add_asset(path)


def _export(image, path):
    fmt = {".png":"PNG", ".jpg":"JPEG", ".jpeg":"JPEG", ".webp":"WEBP"}.get(path.suffix.lower())
    if not fmt:
        raise ValueError("Use a PNG, JPEG or WebP filename.")
    if fmt == "JPEG":
        background = Image.new("RGB", image.size, "white")
        background.paste(image, mask=image.getchannel("A"))
        image = background
    with NamedTemporaryFile(dir=path.parent, delete=False, suffix=path.suffix) as tmp:
        temp = Path(tmp.name)
    try:
        image.save(temp, fmt, **({"quality":95} if fmt=="JPEG" else {"lossless":True} if fmt=="WEBP" else {}))
        try:
            os.link(temp, path)
        except FileExistsError:
            raise ValueError("Choose a new image filename.") from None
    finally:
        temp.unlink(missing_ok=True)
    return {"path":str(path)}


def action(studio, project, op, args):
    doc = project.state()
    rev = doc["revisions"][args["revision"]]
    comp = doc["compositions"][rev["composition"]]
    if op not in ("export","guide") and comp["current"] != rev["id"]:
        raise ValueError("This canvas changed. Reopen the current revision.")
    selection = doc.get("image_selections", {}).get(rev["id"])
    if op == "vision":
        from .image_director import prepare
        return prepare(studio,project,args)
    if op == "selection":
        return {"selection":_selection(project, rev, args)}
    if op == "clear":
        with project.transaction() as state:
            saved = state.setdefault("image_selections", {})
            if (saved.get(rev["id"]) or {}).get("asset") != args.get("previous"):
                raise ValueError("The selection changed. Reload the canvas.")
            saved.pop(rev["id"], None)
        return {}
    if op == "guide":
        slot = args["slot"]
        if slot not in ("first","middle","last"):
            raise ValueError("Choose a delivered frame position.")
        with project.transaction() as state:
            if args.get("shot"):
                shot = state["shots"][args["shot"]]
            else:
                shot = project._shot(rev["name"], 5)
                state["shots"][shot["id"]] = shot
                state["cut"].append(shot["id"])
            last = round(24 * shot["duration"])-1
            frame = {"first":0,"middle":(last+1)//2,"last":last}[slot]
            def same(g):
                return g["frame"] == frame and g.get("role", "image") in ("first","middle","last","image","first_frame","last_frame")
            others = [g for g in shot["guides"] if not same(g)]
            others.append({"frame":frame,"role":"image","usage":"both","revision":rev["id"]})
            shot["guides"] = sorted(others,key=lambda g:g["frame"])
        return {"shot":shot["id"], "frame":frame}
    if op == "export":
        path = Path(args["path"]).resolve()
        if path.is_relative_to((project.folder/"assets").resolve()) or path.is_relative_to((project.folder/"preview-cache").resolve()):
            raise ValueError("Choose an image export location outside project assets and preview cache.")
        return _export(project.render_revision(rev["id"]), path)
    if op == "ai":
        from .image_tools import prepare_image
        task = args["task"]
        if task not in ("edit","remove","cutout","recolor"):
            raise ValueError("Choose an image edit, removal, cutout or recolor.")
        if task != "cutout" and args.get("selection") != (selection or {}).get("asset"):
            raise ValueError("Reopen the AI edit to review its current selection.")
        if task == "remove" and not selection:
            raise ValueError("Draw a selection around the subject first.")
        fields = {k:args[k] for k in ("model","task","prompt","seed","revision","surface") if k in args}
        if note_id := args.get("visual_note"):
            note = doc.get("visual_notes",{}).get(note_id) or {}
            if not note.get("answer") or (note.get("result") or note.get("source"))!=rev["id"]:
                raise ValueError("Open the composition revision used by the Visual Director.")
            if note.get("phase")=="plan" and note.get("selection") and note["selection"]!=(selection or {}).get("asset"):
                raise ValueError("This plan used a different selection. Request a new plan.")
            fields["visual_note"]=note_id
        # Qwen's native cutout supplies full-canvas alpha. A local edit mask would paste the old background back.
        if selection and task != "cutout":
            fields["mask_path"] = str(project.asset_path(selection["asset"]))
        return prepare_image(studio, project, fields, doc)

    layers = rev["layers"]
    width,height = rev["width"],rev["height"]
    if op == "resize":
        from .project import _size
        width, height = args.get("width"), args.get("height")
        _size(width, height)
        image = project.render_revision(rev["id"]).resize((width,height), Image.Resampling.LANCZOS)
        asset = _save_pixels(project, image, "Resized image.png")
        result = project.save_composition(rev["name"]+" · Resized",width,height,[{"name":"Resized image","asset":asset}])
        return {"revision":result}
    if op in ("crop", "cutout"):
        if not selection or args.get("selection") != selection["asset"]:
            raise ValueError("Draw a selection for this revision first.")
        x,y,x2,y2 = selection["bounds"]
        if op == "cutout":
            image = project.render_revision(rev["id"])
            with Image.open(project.asset_path(selection["asset"])) as mask:
                image.putalpha(ImageChops.multiply(image.getchannel("A"),mask.convert("L")))
            cut = image.crop((x,y,x2,y2))
            asset = _save_pixels(project, cut, "Cutout.png")
            result = project.save_composition(rev["name"]+" · Cutout",*cut.size,[{"name":"Cutout","asset":asset}])
            return {"revision":result}
        width,height = x2-x,y2-y
        for layer in layers:
            layer.update(x=layer.get("x",0)-x, y=layer.get("y",0)-y)
    elif op in ("adjust","duplicate","delete","flip"):
        index = args["index"]
        if type(index) is not int or not 0 <= index < len(layers):
            raise ValueError("Select a layer.")
        layer = layers[index]
        if op == "adjust":
            with Image.open(project.asset_path(layer["asset"])) as handle:
                original = ImageOps.exif_transpose(handle).convert("RGBA")
            rgb = original.convert("RGB")
            for name, tool in (("brightness",ImageEnhance.Brightness),("contrast",ImageEnhance.Contrast),
                               ("saturation",ImageEnhance.Color),("sharpness",ImageEnhance.Sharpness)):
                factor = args.get(name,1)
                if type(factor) not in (int,float) or not math.isfinite(factor) or not 0 <= factor <= 3:
                    raise ValueError("Adjustment values must be between 0 and 3.")
                rgb = tool(rgb).enhance(factor)
            rgb.putalpha(original.getchannel("A"))
            layer["asset"] = _save_pixels(project, rgb, "Adjusted layer.png")
        elif op == "duplicate":
            layers.insert(index+1, {**layer,"name":layer.get("name","Layer")+" copy"})
        elif op == "delete":
            if len(layers)==1:
                raise ValueError("Keep at least one layer in this composition.")
            layers.pop(index)
        else:
            axis = args["axis"]
            if axis not in ("x","y"):
                raise ValueError("Choose a flip axis.")
            layer["flip_"+axis] = not layer.get("flip_"+axis,False)
    else:
        raise ValueError("Unknown Image workspace operation.")
    result = project.save_composition(rev["name"],width,height,layers,rev["composition"],base_revision=rev["id"])
    return {"revision":result}
