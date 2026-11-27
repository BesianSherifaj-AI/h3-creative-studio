"""Describe exact files used by Studio's recipes, with optional checks against a running ComfyUI."""
import json
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

import httpx

from . import image_director
from vendor.image_graph import helpers, workflows
from vendor.prompt import comfy_transfer, mmh3_transfer
from vendor.prompt.resources import local_url


@lru_cache(maxsize=1)
def _sources():
    return json.loads(Path(__file__).with_name("model-sources.json").read_text(encoding="utf-8"))


def publisher_url(filename):
    entry = _sources().get(filename)
    if entry:
        return f"https://huggingface.co/{entry['repo']}/blob/{entry['revision']}/{quote(entry['path'])}"
    return None


def _recipes(kind):
    if kind == 'video':
        for mode,title in (('fl2va','Text and frame-guided motion'),('ref2va','Reference media motion')):
            graph,_ = comfy_transfer._template(mode)
            yield title,graph,set()
        yield 'Saved motion and continuation',{},mmh3_transfer.SAVE_NODES | mmh3_transfer.CONTINUATION_NODES
    elif kind == 'image':
        for model,settings in workflows.REGISTRY.items():
            task = 'layers' if model == 'layered' else 'create'
            spec = dict(model=model,task=task,width=512,height=512,seed=1,prompt='setup check')
            yield settings['name'],workflows.build(spec,'check',source='input.png' if task=='layers' else None),set()
        spec = dict(model='zimage',task='control',width=512,height=512,seed=1,prompt='setup check')
        yield 'Z-Image ControlNet',workflows.build(spec,'check',control='control.png'),set()
        for model,title in (('sam','SAM object selection'),('seedvr2','SeedVR2 enhancement')):
            yield title,helpers.build(dict(model=model,scale=2,prompt='object'),'input.png','check'),set()
        for model in image_director.VISION_MODELS:
            label = model.removeprefix("qwen3vl_").removesuffix(".safetensors").replace("_", " ").upper()
            yield "Visual Director / "+label,image_director.graph(model,["input.png"],"setup check","check"),set()
    else:
        raise ValueError('Choose the image or video renderer.')


def report(kind,url):
    base = local_url(url)
    schema = None
    try:
        with httpx.Client(trust_env=False,timeout=5) as client:
            response = client.get(base+'/object_info')
            response.raise_for_status()
            info = response.json()
            if isinstance(info,dict) and 'LoadImage' in info:
                schema = info
    except (httpx.HTTPError,ValueError):
        pass
    folders = {'unet_name':'diffusion_models','clip_name':'text_encoders','vae_name':'vae',
               'ckpt_name':'checkpoints','lora_name':'loras','name':'model_patches'}
    results = []
    for title,graph,extras in _recipes(kind):
        nodes = extras | {node['class_type'] for node in graph.values()}
        absent = sorted(nodes - schema.keys()) if schema is not None else []
        files = []
        for node in graph.values():
            for key,value in node['inputs'].items():
                if key not in folders or not isinstance(value,str) or not value.endswith('.safetensors'):
                    continue
                fields = (schema or {}).get(node['class_type'],{}).get('input',{}).get('required',{})
                choices = mmh3_transfer._choices(fields.get(key))
                installed = value in choices if schema is not None else None
                files.append(dict(name=value,folder=folders[key],installed=installed,
                                  bytes=_sources().get(value,{}).get("bytes")))
        results.append(dict(title=title,files=files,missing_nodes=absent))
    return dict(connected=schema is not None,url=base,recipes=results)
