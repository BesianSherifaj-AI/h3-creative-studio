"""Build a WebView-compatible cut preview from the immutable source take."""
from pathlib import Path

from . import media


def take_preview(project, take_id):
    take = project.state()['takes'][take_id]
    options = take['settings']
    source = project.asset_path(take['video_asset'])
    delivery = options.get('delivery_trim')
    if not delivery:
        return project.asset_path(options['preview_asset']) if options.get('preview_asset') else source
    cache = project.folder / 'preview-cache'
    cache.mkdir(exist_ok=True)
    output = cache / (take_id + '-cut.mp4')
    if not output.exists():
        meta = media.probe_media(source)
        factor = min(1,1280/max(meta['width'],meta['height']))
        width, height = [max(2,round(meta[k]*factor/2)*2) for k in ('width','height')]
        clip = dict(path=source,start=delivery[0],end=delivery[1])
        routing = options.get('audio_route')
        if routing:
            clip['audio'] = routing[:1]
        media.export_film([clip],output,width=width,height=height,fps='24',color='bt709',audio_tracks=1)
    return output
