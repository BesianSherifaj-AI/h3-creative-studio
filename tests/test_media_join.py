"""Upstream MIT synthetic join fixtures adapted from H3 Prompt Studio (see vendor/prompt/LICENSE)."""
import copy
import pytest
from h3studio.media import prepare_h3_join

def node_schema(required, outputs, optional=None, output_node=False):
    return {'input': {'required': required, 'optional': optional or {}},
            'output': outputs, 'output_node': output_node}


def base_schema():
    def spec(t):
        return [t, {}]
    return {
        "VAELoader": node_schema({"vae_name": [["minimax_h3_video_vae_fp16.safetensors", "minimax_h3_audio_vae_fp32.safetensors"]]}, ["VAE"]),
        "VAEDecode": node_schema({"samples": spec("LATENT"),"vae": spec("VAE")}, ["IMAGE"]),
        "VAEDecodeAudio": node_schema({"samples": spec("LATENT"),"vae": spec("VAE")}, ["AUDIO"]),
        "CreateVideo": node_schema({"images": spec("IMAGE"),"audio": spec("AUDIO"),"fps": spec("FLOAT"), "bit_depth":[[8,10]],"color_space":[["sRGB"]]}, ["VIDEO"]),
        "SaveVideo": node_schema({"video": spec("VIDEO"),"filename_prefix":spec("STRING"),"format":[["mp4"]],"format.codec":[["h264"]]}, [], output_node=True),
    }


def schema():
    result = base_schema()
    result['MMH3Load'] = {**node_schema({
        'file': [['(none)', *[f'output::mmh3/clip-{n}.mmh3' for n in range(1, 5)]]],
        'verify': [['on_access', 'manifest', 'full']], 'path_override': ['STRING', {}]}, ['MMH3_MEDIA']), 'output_name': ['packet']}
    result['MMH3H3LatentStitch'] = {**node_schema({'segments': ['COMFY_AUTOGROW_V3', {'template': {
        'input': {'required': {'segment': ['MMH3_MEDIA', {}]}}, 'names': [f'segment_{n}' for n in range(1, 67)], 'min': 2}}]},
        ['MMH3_MEDIA', 'LATENT', 'STRING', 'STRING']), 'output_name': ['packet', 'latent', 'summary', 'assembly_report_json']}
    return result


def decoded_schema(schema):
    result = copy.deepcopy(schema)
    # Current native CreateVideo permits a picture-only intermediate stream.
    create_inputs = result['CreateVideo']['input']
    create_inputs['optional']['audio'] = create_inputs['required'].pop('audio')
    result['MMH3Unpack'] = {**node_schema({'packet': ['MMH3_MEDIA', {}]}, ['LATENT', 'VIDEO', 'AUDIO']),
                           'output_name': ['latent', 'video', 'audio']}
    result['Video Slice'] = {**node_schema({'video': ['VIDEO', {}], 'start_time': ['FLOAT', {}],
                            'duration': ['FLOAT', {}], 'strict_duration': ['BOOLEAN', {}]}, ['VIDEO']), 'output_name': ['VIDEO']}
    result['GetVideoComponents'] = {**node_schema({'video': ['VIDEO', {}]}, ['IMAGE', 'AUDIO', 'FLOAT', 'COMBO', 'COMBO']),
                                    'output_name': ['images', 'audio', 'fps', 'bit_depth', 'color_space']}
    result['TrimAudioDuration'] = {**node_schema({'audio': ['AUDIO', {}], 'start_index': ['FLOAT', {}],
                                    'duration': ['FLOAT', {}]}, ['AUDIO']), 'output_name': ['AUDIO']}
    result['MMH3PackH3Result'] = {**node_schema({'packet': ['MMH3_MEDIA', {}], 'video': ['VIDEO', {}],
        'audio': ['AUDIO', {}], 'operation': ['STRING', {}], 'mode': ['STRING', {}], 'status': ['STRING', {}],
        'process_info_json': ['STRING', {}], 'latent_origin': [['derived', 'sampler_output']]}, ['MMH3_MEDIA']), 'output_name': ['packet']}
    fields = copy.deepcopy(result['MMH3H3LatentStitch']['input']['required'])
    fields.update(transition=[['Cut', 'Crossfade', 'Auto Seamless']], transition_frames=['INT', {}],
                  color_match=[['Off', 'Auto', 'Always']], color_match_strength=['FLOAT', {}], color_match_decay_frames=['INT', {}])
    result['MMH3VideoStitch'] = {**node_schema(fields, ['MMH3_MEDIA', 'VIDEO', 'AUDIO', 'STRING', 'STRING']),
                               'output_name': ['packet', 'video', 'audio', 'summary', 'assembly_report_json']}
    return result


def records(count=2):
    result = []
    for index in range(1, count + 1):
        graph = {
            '1': {'class_type': 'VAELoader', 'inputs': {'vae_name': 'minimax_h3_video_vae_fp16.safetensors'}},
            '2': {'class_type': 'VAELoader', 'inputs': {'vae_name': 'minimax_h3_audio_vae_fp32.safetensors'}},
            '3': {'class_type': 'VAEDecode', 'inputs': {'samples': ['sampler', 0], 'vae': ['1', 0]}},
            '4': {'class_type': 'VAEDecodeAudio', 'inputs': {'samples': ['sampler', 0], 'vae': ['2', 0]}},
            '5': {'class_type': 'CreateVideo', 'inputs': {'images': ['3', 0], 'audio': ['4', 0], 'fps': 24, 'bit_depth': 8, 'color_space': 'sRGB'}},
            '6': {'class_type': 'SaveVideo', 'inputs': {'video': ['5', 0]}},
        }
        media = {'latent_origin': 'sampler_output', 'continuation': index > 1, 'save_enabled': True}
        if index > 1:
            parent = f'mmh3/clip-{index-1}.mmh3'
            media.update(source=parent, overlap_frames=39)
            graph.update({
                '7': {'class_type': 'MMH3Load', 'inputs': {'file': 'output::' + parent, 'path_override': ''}},
                '8': {'class_type': 'MMH3H3ContinuationHandover', 'inputs': {'packet': ['7', 0], 'video_handover_frames': 39,
                    'audio_handover_frames': 0, 'audio_feather_frames': 0}},
            })
        result.append({'run': {'id': f'run-{index}', 'project_id': f'project-{index}', 'status': 'succeeded',
            'continuation_source': f'mmh3/clip-{index}.mmh3', 'parent_run_id': 'reroll-pointer-is-not-proof'},
            'transfer': {'id': f'transfer-{index}', 'comfy_url': 'http://127.0.0.1:8010', 'prompt': graph,
                'manifest': {'project_id': f'project-{index}', 'mode': 'ref2va', 'width': 736, 'height': 416, 'frames': 124, 'fps': 24, 'mmh3': media}}})
    return result


def mixed_records():
    entries = records(4)
    for entry, frames in zip(entries, (124, 124, 107, 107)):
        entry['transfer']['manifest']['frames'] = frames
        entry['transfer']['manifest']['mmh3']['pack_node_id'] = '9'
        entry['transfer']['prompt']['9'] = {'class_type': 'MMH3PackH3Result',
                                            'inputs': {'video': ['5', 0], 'audio': ['4', 0]}}
    return entries


def test_original_packet_lineage_and_audio_phase_routes():
    inputs = records()
    saved = copy.deepcopy(inputs)
    nodes = decoded_schema(schema())
    normal = prepare_h3_join(inputs,nodes)
    assert normal["manifest"]["assembly_mode"] == "latent"
    assert normal["manifest"]["frames"] == 209
    assert inputs == saved
    fallback = prepare_h3_join(mixed_records(),nodes)
    assert fallback["manifest"]["assembly_mode"] == "streaming_decoded"
    assert fallback["manifest"]["audio_boundary_preflight"]["boundaries"][2]["shortfall_ticks"] == 1
    inputs[1]["transfer"]["manifest"]["mmh3"]["source"]="mmh3/unrelated.mmh3"
    with pytest.raises(ValueError):
        prepare_h3_join(inputs,nodes)
