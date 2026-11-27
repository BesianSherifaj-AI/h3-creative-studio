"""Synthetic graph and transport fixtures adapted from H3 Prompt Studio's MIT tests."""
import contextlib
import copy
import json
import subprocess

import httpx
import pytest
from PIL import Image

from h3studio.generation import prepare_take, guide_layout, timing, validate_saved_take
from vendor.prompt.comfy_transfer import HERETIC, REF_LORA, FL_LORA


def node_schema(required, outputs, optional=None, output_node=False):
    return {'input': {'required': required, 'optional': optional or {}},
            'output': outputs, 'output_node': output_node}


def base_schema():
    def enum(*values):
        return [list(values)]
    def spec(kind):
        return [kind, {}]
    def model_inputs(**kwargs):
        return {'model': spec('MODEL'), **kwargs}
    inputs = {'clip': spec('CLIP'), 'vae': spec('VAE'), 'prompt': spec('STRING'),
              'width': ['INT', {'min': 32, 'max': 16384}], 'height': ['INT', {'min': 32, 'max': 16384}],
              'length': ['INT', {'min': 5, 'max': 3600}]}
    ref_inputs = {**inputs, 'ref_image_size': ['COMBO', {'options': ['match', 'max'], 'default': 'match'}]}
    del ref_inputs['vae']
    autogrow = ['COMFY_AUTOGROW_V3', {'template': {'input': {'required': {'ref_image': spec('IMAGE')}}},
                                     'prefix': 'ref_image_', 'min': 0, 'max': 9}]
    codec = ['COMFY_DYNAMICCOMBO_V3', {'options': [
        {'key': 'h264', 'inputs': {'optional': {'encoding': ['COMFY_DYNAMICCOMBO_V3', {'options': [{'key': 'auto', 'inputs': {'required': {}}}]}]}}}]}]
    format_spec = ['COMFY_DYNAMICCOMBO_V3', {'options': [{'key': 'mp4', 'inputs': {'required': {'codec': codec}}}]}]
    return {
        'UNETLoader': node_schema({'unet_name': enum('minimax_h3_ref2va_pruned_int8_convrot.safetensors', 'minimax_h3_fl2va_pruned_int8_convrot.safetensors'), 'weight_dtype': enum('default')}, ['MODEL']),
        'CLIPLoader': node_schema({'clip_name': enum(HERETIC), 'type': enum('minimax'), 'device': enum('default', 'cpu')}, ['CLIP']),
        'VAELoader': node_schema({'vae_name': enum('minimax_h3_video_vae_fp16.safetensors', 'minimax_h3_audio_vae_fp32.safetensors')}, ['VAE']),
        'LoraLoaderModelOnly': node_schema(model_inputs(lora_name=enum(REF_LORA, FL_LORA), strength_model=spec('FLOAT')), ['MODEL']),
        'MiniMaxH3SigmaShift': node_schema(model_inputs(shift_video=spec('FLOAT'), shift_audio=spec('FLOAT')), ['MODEL']),
        'H3SLAAttention': node_schema(model_inputs(sparsity_ratio=spec('FLOAT'), block_size=enum('32'), min_seq_len=spec('INT'),
            dense_last_steps=spec('INT'), protect_audio=spec('BOOLEAN'), enabled=spec('BOOLEAN'), dense_steps=spec('STRING'),
            dense_backend=enum('comfy_kitchen'), disable_fp16_accum=spec('BOOLEAN'), stabilize_motion=spec('BOOLEAN'),
            reference_protection=enum('Heavy Enforcement'), tail_correction=spec('BOOLEAN'), use_int8_qk=spec('BOOLEAN'), engine=enum('comfy_kitchen')), ['MODEL']),
        'MiniMaxH3ReferenceToVideo': node_schema(ref_inputs, ['CONDITIONING', 'LATENT'], {'vae': spec('VAE'), 'audio_vae': spec('VAE'), 'ref_images': autogrow}),
        'MiniMaxH3ImageToVideo': node_schema(inputs, ['CONDITIONING', 'LATENT'], {'first_frame': spec('IMAGE'), 'last_frame': spec('IMAGE')}),
        'LoadImage': node_schema({'image': enum('unrelated-user-photo.png')}, ['IMAGE', 'MASK']),
        'BasicGuider': node_schema(model_inputs(conditioning=spec('CONDITIONING')), ['GUIDER']),
        'RandomNoise': node_schema({'noise_seed': ['INT', {'control_after_generate': True, 'min': 0}]}, ['NOISE']),
        'KSamplerSelect': node_schema({'sampler_name': enum('euler')}, ['SAMPLER']),
        'BasicScheduler': node_schema(model_inputs(scheduler=enum('simple'), steps=['INT', {'min': 1, 'max': 10000}], denoise=spec('FLOAT')), ['SIGMAS']),
        'SamplerCustomAdvanced': node_schema({'noise': spec('NOISE'), 'guider': spec('GUIDER'), 'sampler': spec('SAMPLER'), 'sigmas': spec('SIGMAS'), 'latent_image': spec('LATENT')}, ['LATENT', 'LATENT']),
        'VAEDecode': node_schema({'samples': spec('LATENT'), 'vae': spec('VAE')}, ['IMAGE']),
        'VAEDecodeAudio': node_schema({'samples': spec('LATENT'), 'vae': spec('VAE')}, ['AUDIO']),
        'CreateVideo': node_schema({'images': spec('IMAGE'), 'audio': spec('AUDIO'), 'fps': spec('FLOAT'), 'bit_depth': [ [8, 10] ], 'color_space': enum('sRGB')}, ['VIDEO']),
        'SaveVideo': node_schema({'video': spec('VIDEO'), 'filename_prefix': spec('STRING'), 'format': format_spec}, [], output_node=True),
    }


class FakeComfy:
    def __init__(self, schema):
        self.schema, self.calls, self.uploads = schema, [], {}
        self.corrupt = False
        self.wrong_location = False
        self.offline = False

    def get(self, url, **kwargs):
        self.calls.append(('GET', url, kwargs))
        req = httpx.Request('GET', url)
        if self.offline:
            raise httpx.ConnectError('Offline', request=req)
        if url.endswith('/object_info'):
            return httpx.Response(200, json=self.schema, request=req)
        if url.endswith('/view'):
            p = kwargs['params']
            data = self.uploads[(p['subfolder'], p['filename'])]
            return httpx.Response(200, content=b'changed' if self.corrupt else data, request=req)
        raise AssertionError(f'Unexpected read: {url}')

    def stream(self, method, url, **kwargs):
        assert method == 'GET'
        return contextlib.nullcontext(self.get(url, **kwargs))

    def post(self, url, **kwargs):
        self.calls.append(('POST', url, kwargs))
        assert url.endswith('/upload/image'), 'Transfer must never call prompt, free, queue or interrupt'
        filename, data, mime = kwargs['files']['image']
        options = kwargs['data']
        assert options['overwrite'] == 'false'
        assert options['type'] == 'input'
        self.uploads[(options['subfolder'], filename)] = data.read() if hasattr(data, 'read') else data
        return httpx.Response(200, request=httpx.Request('POST', url), json={
            'name': filename, 'subfolder': 'unexpected' if self.wrong_location else options['subfolder'], 'type': 'input'})


def schema():
    result = base_schema()

    def s(kind, **options): return [kind, options]
    def enum(*values): return [list(values)]
    def node(required, outputs, names, optional=None, output_node=False):
        return {**node_schema(required, outputs, optional, output_node), 'output_name': names}

    grow = ['COMFY_AUTOGROW_V3', {'template': {'input': {'required': {'image': s('IMAGE')}}, 'prefix': 'image_', 'min': 0, 'max': 10}}]
    task = ['COMFY_DYNAMICCOMBO_V3', {'options': [
        {'key': 'Video (optional frames)', 'inputs': {}},
        {'key': 'References to video', 'inputs': {'optional': {'reference_images': grow}}},
    ]}]
    result.update({
        'MMH3Create': node({'prompt': s('STRING'), 'task': task, 'seed': s('INT'), 'name': s('STRING'), 'notes': s('STRING')},
                           ['MMH3_MEDIA'], ['packet'], {'first_frame': s('IMAGE'), 'last_frame': s('IMAGE')}),
        'MMH3Inspect': node({'packet': s('MMH3_MEDIA')},
                           ['STRING', 'STRING', 'STRING', 'INT', 'INT', 'STRING', 'FLOAT', 'INT', 'FLOAT', 'FLOAT',
                            'BOOLEAN', 'STRING', 'STRING', 'INT', 'BOOLEAN', 'STRING'],
                           ['summary', 'info_json', 'notes', 'width', 'height', 'aspect_ratio', 'aspect_ratio_value',
                            'frames', 'fps', 'duration', 'has_latent', 'prompt', 'task', 'seed', 'seed_recorded', 'name']),
        'MMH3PackH3Result': node({'packet': s('MMH3_MEDIA'), 'operation': s('STRING'), 'mode': s('STRING'), 'status': s('STRING'),
                                  'process_info_json': s('STRING'), 'latent_origin': enum('sampler_output', 'vae_encoded', 'derived', 'unknown')},
                                 ['MMH3_MEDIA', 'STRING', 'STRING'], ['packet', 'summary', 'result_info_json'],
                                 {'latent': s('LATENT'), 'video': s('VIDEO'), 'audio': s('AUDIO'), 'applied_loras_json': s('STRING'),
                                  'generation_settings_json': s('STRING')}),
        'MMH3Save': node({'packet': s('MMH3_MEDIA'), 'filename_prefix': s('STRING'), 'target': enum('output', 'source (in-place)'),
                          'overwrite': s('BOOLEAN')}, ['MMH3_MEDIA', 'STRING'], ['packet', 'path'], output_node=True),
        'MMH3Load': node({'file': enum('(none)', 'output::mmh3/source.mmh3', 'input::other.mmh3'),
                          'verify': enum('on_access', 'manifest', 'full'), 'path_override': s('STRING')}, ['MMH3_MEDIA'], ['packet']),
        'MMH3H3GenerationSettings': node({'packet': s('MMH3_MEDIA'), 'resolution': enum('Source'), 'duration_seconds': s('FLOAT')},
                                         ['MMH3_MEDIA', 'INT', 'INT', 'INT', 'STRING'], ['packet', 'width', 'height', 'frames', 'settings_json']),
        'MMH3H3ContinuationHandover': node({'packet': s('MMH3_MEDIA'), 'target_frames': s('INT'), 'video_handover_frames': s('INT'),
                                            'audio_handover_frames': s('INT'), 'audio_feather_frames': s('INT')},
                                           ['MMH3_MEDIA', 'LATENT', 'STRING', 'STRING'], ['packet', 'latent', 'summary', 'info_json'],
                                           {'target_width': s('INT', forceInput=True), 'target_height': s('INT', forceInput=True)}),
        'MMH3H3ContinuationCondition': node({'packet': s('MMH3_MEDIA'), 'handover_info_json': s('STRING', forceInput=True),
                                             'task_family': enum('auto', 'fl2va', 'ref2va'), 'prompt_override': s('STRING'), 'seed_override': s('INT'),
                                             'clip': s('CLIP'), 'video_vae': s('VAE'), 'width_override': s('INT', forceInput=True),
                                             'height_override': s('INT', forceInput=True), 'frames_override': s('INT', forceInput=True)},
                                            ['CONDITIONING', 'INT', 'MMH3_MEDIA', 'STRING', 'STRING', 'STRING', 'COMBO'],
                                            ['positive', 'seed', 'packet', 'mode', 'status', 'info_json', 'task_family'], {'audio_vae': s('VAE')}),
    })
    return result


class MMH3Comfy(FakeComfy):
    def __init__(self, schema):
        super().__init__(schema)
        self.card = {'id': 'source-packet-id', 'name': 'Source film', 'revision': 'saved-revision',
                     'geometry': {'width': 960, 'height': 544, 'frames': 362, 'fps': 24, 'duration': 362 / 24},
                     'has': {'latent': True, 'video': True}}

    def get(self, url, **kwargs):
        if url.endswith('/mmh3_media/file_info'):
            self.calls.append(('GET', url, kwargs))
            assert kwargs['params'] == {'file': 'output::mmh3/source.mmh3'}
            return httpx.Response(200, json=self.card, request=httpx.Request('GET', url))
        return super().get(url, **kwargs)

def setup_case(tmp_path, mode="t2va", parent=False):
    data = schema()
    data["MiniMaxH3AddGuide"] = node_schema({"positive": ["CONDITIONING"], "latent": ["LATENT"], "frame_idx": ["INT"]},
                                             ["CONDITIONING"], {"image": ["IMAGE"], "audio": ["AUDIO"], "vae": ["VAE"], "audio_vae": ["VAE"]})
    fake = MMH3Comfy(data)
    path = tmp_path / "still.png"
    Image.new("RGB", (64, 48), "teal").save(path)
    spec = {"prompt": "The camera follows a paper boat.", "comfy_url": "http://127.0.0.1:8010",
            "duration": 5, "mode": mode, "seed": 41, "settings": {"save_mmh3": True},
            "guides": [{"asset_id": "canvas1", "frame": x, "kind": "image", "revision": "v2"} for x in (0, 60, 119)]}
    if mode == "fl2va":
        spec["assets"] = [{"id": k, "role": k, "media_type": "image", "revision": "v1"} for k in ("first_frame", "last_frame")]
    if parent:
        spec["parent"] = {"take_id": "take", "packet_id": "source-packet-id", "packet_revision": "saved-revision",
                          "selector": "output::mmh3/source.mmh3", "trim": [0,120], "render_frames": 124, "use_original_motion": True}
    return spec, fake, lambda _: path


@pytest.mark.parametrize("continued", [False, True])
def test_mixed_media_guides_new_continued_and_reload(tmp_path, continued):
    spec, fake, _ = setup_case(tmp_path, parent=continued)
    fake.schema.update({
        "LoadVideo": node_schema({"file": [["example.mp4"]]}, ["VIDEO"]),
        "GetVideoComponents": node_schema({"video": ["VIDEO"]}, ["IMAGE","AUDIO","FLOAT"]),
        "LoadAudio": node_schema({"audio": [["example.wav"]]}, ["AUDIO"]),
    })
    files = {}
    for length in (4, 27):
        dest = tmp_path / f"{length}.mp4"
        subprocess.run(["ffmpeg","-loglevel","error","-f","lavfi","-i","color=red:s=64x48:r=24",
                        "-f","lavfi","-i","sine=frequency=440:sample_rate=48000","-frames:v",str(length),
                        "-c:v","libx264","-c:a","aac","-t",str(length/24),str(dest)],check=True)
        files[str(length)] = dest
    sound = tmp_path / "sound.wav"
    subprocess.run(["ffmpeg","-loglevel","error","-f","lavfi","-i","sine=frequency=1000:duration=2",str(sound)],check=True)
    files["sound"] = sound
    spec["guides"] = [{"asset_id":"4", "kind":"video", "frame":0, "with_audio":True},
                      {"asset_id":"27", "kind":"video", "frame":35},
                      {"asset_id":"sound", "kind":"audio", "frame":119}]
    bundle = prepare_take(spec, files.__getitem__, client=fake)
    manifest = bundle["manifest"]
    assert [g["retained_frames"] for g in manifest["guides"]] == [1,22,1]
    assert manifest["guides"][-1]["available_audio_ticks"] == (28 if continued else 8)
    for g in manifest["guides"]:
        node = bundle["prompt"][g["node_id"]]["inputs"]
        assert ("audio" in node) == (g["asset_id"] in ("4","sound"))
        assert ("image" in node) == (g["kind"] == "video")
    assert validate_saved_take(json.loads(json.dumps(bundle)), client=fake)
    if continued:
        fake.card["revision"] = "replaced-after-preparation"
        with pytest.raises(ValueError, match="parent packet changed"):
            validate_saved_take(bundle, client=fake)
        fake.card["revision"] = spec["parent"]["packet_revision"]
    spec["guides"][1]["frame"] = 99
    with pytest.raises(ValueError, match="crosses the end"):
        prepare_take(spec, files.__getitem__, client=fake)


@pytest.mark.parametrize("mode,parent,end", [("t2va", False, 119), ("fl2va", False, 119), ("t2va", True, 158)])
def test_take_reload_and_owned_guide_branch(tmp_path, mode, parent, end):
    spec, fake, resolve = setup_case(tmp_path, mode, parent)
    bundle = prepare_take(spec, resolve, client=fake)
    manifest = bundle["manifest"]
    assert manifest["timeline"]["last"] == end
    assert manifest["timeline"]["render_frames"] == (175 if parent else 124)
    assert manifest["seed"] == 41
    assert manifest["guides"][-1]["frame_idx"] == end
    assert manifest["guides"][-1]["revision"] == "v2"
    graph = bundle["prompt"]
    sampler = next(n for n in graph.values() if n["class_type"] == "SamplerCustomAdvanced")
    guider = next(n for n in graph.values() if n["class_type"] == "BasicGuider")
    pos = guider["inputs"]["conditioning"]
    for item in reversed(manifest["guides"]):
        node = graph[pos[0]]
        assert pos[0] == item["node_id"] and node["inputs"]["latent"] == sampler["inputs"]["latent_image"]
        pos = node["inputs"]["positive"]
    assert pos[0] == manifest["conditioning_node_id"]
    if mode == "fl2va":
        assert "last_frame" not in graph[pos[0]]["inputs"]
        assert manifest["guides"][0]["role"] == "last_frame"
    reloaded = json.loads(json.dumps(bundle))
    assert validate_saved_take(reloaded, client=fake)
    changed = reloaded["manifest"]["guides"][-1]
    reloaded["prompt"][changed["node_id"]]["inputs"]["frame_idx"] += 1
    with pytest.raises(ValueError, match="timed guide"):
        validate_saved_take(reloaded, client=fake)


def test_model_removed_and_parent_mismatch(tmp_path):
    spec, fake, resolve = setup_case(tmp_path, parent=True)
    spec["parent"]["packet_revision"] = "different"
    with pytest.raises(ValueError, match="revision"):
        prepare_take(spec, resolve, client=fake)
    spec["parent"]["use_original_motion"] = False
    with pytest.raises(ValueError, match="original motion"):
        prepare_take(spec, resolve, client=fake)
    spec.pop("parent")
    result = prepare_take(spec, resolve, client=fake)
    fake.schema["LoraLoaderModelOnly"]["input"]["required"]["lora_name"] = [["another.safetensors"]]
    with pytest.raises(ValueError, match="lora_name"):
        validate_saved_take(result, client=fake)


@pytest.mark.parametrize("overlap", [0,39])
def test_lengths_crop_and_audio(overlap):
    duration = timing(5, overlap)
    assert guide_layout(119, 4, duration)["retained_frames"] == 1
    assert guide_layout(3, 36, duration)["retained_frames"] == 22
    with pytest.raises(ValueError, match="crosses"):
        guide_layout(119, 5, duration)
    end = guide_layout(119, 1, duration, [200,100], [100,100], audio_seconds=8)
    assert end["source_crop_xywh"] == [50,0,100,100]
    assert end["delivered_audio_seconds"] == pytest.approx(1/24)
    assert end["available_audio_ticks"] == (28 if overlap else 8)
    assert end["render_audio_seconds"] == end["available_audio_ticks"] / 40
    assert guide_layout(0,1,duration,[201,100],[100,100])["source_crop_xywh"] == [50,0,101,100]
