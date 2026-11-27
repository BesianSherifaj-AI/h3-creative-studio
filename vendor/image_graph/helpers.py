"""SeedVR2 enhancement and SAM 3.1 still selection from Atelier (see THIRD_PARTY_NOTICES.md)."""
from .workflows import node

SAM = "sam3.1_multiplex_fp16.safetensors"


def build(spec,source,job_id):
    g={'2':node('LoadImage',image=source)}
    if spec['model']=='seedvr2':
        g.update({'1':node('UNETLoader',unet_name='seedvr2_3b_int8_convrot.safetensors',weight_dtype='default'),
          '3':node('VAELoader',vae_name='seedvr2_ema_vae_fp16.safetensors'),
          '4':node('JoinImageWithAlpha',image=['2',0],alpha=['2',1]),
          '5':node('ImageScaleBy',image=['4',0],scale_by=spec['scale'],upscale_method='lanczos'),
          '6':node('SeedVR2Preprocess',resized_images=['5',0]),
          '7':node('VAEEncodeTiled',pixels=['6',0],vae=['3',0],tile_size=512,overlap=128,temporal_size=4096,temporal_overlap=8),
          '8':node('SeedVR2Conditioning',model=['1',0],vae_conditioning=['7',0]),
          '9':node('KSampler',model=['1',0],positive=['8',0],negative=['8',1],latent_image=['7',0],seed=spec.get('seed',42),steps=1,cfg=1,sampler_name='euler',scheduler='simple',denoise=1),
          '10':node('VAEDecodeTiled',samples=['9',0],vae=['3',0],tile_size=512,overlap=128,temporal_size=4096,temporal_overlap=8),
          '11':node('SeedVR2PostProcessing',images=['10',0],original_resized_images=['5',0],color_correction_method='lab'),
          '12':node('SaveImage',images=['11',0],filename_prefix=f'studio/{job_id}')})
        return g
    if spec['model'] != 'sam':
        raise ValueError('Choose the SAM or SeedVR2 model.')
    g['1']=node('CheckpointLoaderSimple',ckpt_name=SAM)
    inputs=dict(model=['1',0],image=['2',0],threshold=spec.get('threshold',.5),refine_iterations=spec.get('refine_iterations',2),individual_masks=False)
    if spec.get('positive_points'):
        import json
        for key,points,number in (('positive_coords',spec['positive_points'],'7'),('negative_coords',spec.get('negative_points',[]),'8')):
            if points:
                g[number]=node('PrimitiveString',value=json.dumps(points))
                inputs[key]=[number,0]
    else:
        g['3']=node('CLIPTextEncode',clip=['1',1],text=spec['prompt'])
        inputs['conditioning']=['3',0]
    g['4']=node('SAM3_Detect',**inputs)
    g['5']=node('MaskToImage',mask=['4',0])
    g['6']=node('SaveImage',images=['5',0],filename_prefix=f'studio/{job_id}')
    return g
