"""Layer transforms and W3C source-over blending from H3 Image Editor. See LICENSE."""
import math
import numpy as np
from PIL import Image, ImageOps

def blend_layer(canvas,source,xy,mode):
    """W3C source-over blending in sRGB, matching Canvas blend modes and alpha."""
    if mode=='normal':
        canvas.alpha_composite(source,xy);return
    x,y=xy;left=max(0,x);top=max(0,y);right=min(canvas.width,x+source.width);bottom=min(canvas.height,y+source.height)
    if right<=left or bottom<=top:return
    bounds=(left,top,right,bottom)
    base=np.asarray(canvas.crop(bounds),dtype=np.float32)/255
    src=np.asarray(source.crop((left-x,top-y,right-x,bottom-y)),dtype=np.float32)/255
    cb,cs=base[:,:,:3],src[:,:,:3];ab,as_=base[:,:,3:4],src[:,:,3:4]
    if mode=='multiply':mixed=cb*cs
    elif mode=='screen':mixed=1-(1-cb)*(1-cs)
    elif mode=='overlay':mixed=np.where(cb<=.5,2*cb*cs,1-2*(1-cb)*(1-cs))
    elif mode=='darken':mixed=np.minimum(cb,cs)
    elif mode=='lighten':mixed=np.maximum(cb,cs)
    elif mode=='difference':mixed=np.abs(cb-cs)
    else:raise ValueError('Unsupported layer blend mode.')
    alpha=as_+ab*(1-as_)
    premult=as_*(1-ab)*cs+as_*ab*mixed+(1-as_)*ab*cb
    rgb=np.divide(premult,alpha,out=np.zeros_like(premult),where=alpha>0)
    rgba=np.concatenate((rgb,alpha),axis=2)
    canvas.paste(Image.fromarray(np.clip(np.rint(rgba*255),0,255).astype(np.uint8)),(left,top))

def transformed_layer(layer,load,apply_opacity=True):
        im=load(layer["asset"]).copy()
        scale=float(layer.get("scale",1));opacity=float(layer.get("opacity",1))
        if not math.isfinite(scale) or not .05<=scale<=4 or not 0<=opacity<=1:raise ValueError("Invalid layer transform.")
        tw,th=max(1,round(im.width*scale)),max(1,round(im.height*scale))
        if tw*th>24_000_000:raise ValueError("Layer is too large.")
        im=im.resize((tw,th),Image.Resampling.LANCZOS)
        if layer.get('flip_x'):im=ImageOps.mirror(im)
        if layer.get('flip_y'):im=ImageOps.flip(im)
        angle=float(layer.get("rotation",0))
        if not math.isfinite(angle):raise ValueError("Invalid rotation.")
        im=im.rotate(-angle,resample=Image.Resampling.BICUBIC,expand=True)
        if layer.get("color"):
            color=layer["color"]
            tint=Image.new("RGBA",im.size,color)
            tint.putalpha(im.getchannel("A"));im=tint
        if apply_opacity:im.putalpha(im.getchannel("A").point(lambda a:round(a*opacity)))
        x,y=int(layer.get("x",0)),int(layer.get("y",0))
        if abs(x)>8192 or abs(y)>8192:raise ValueError("Layer position out of range.")
        # Match canvas preview: rotate about the scaled image's center.
        return im,(round(x+(tw-im.width)/2),round(y+(th-im.height)/2))
