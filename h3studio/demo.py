"""Build a portable, editable synthetic film fixture with original raster layers."""
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from .project import Project


def _noise(rng, size, scale):
    width, height = size
    grid = rng.integers(0, 255, (max(3, height // scale), max(3, width // scale)), dtype=np.uint8)
    return np.asarray(Image.fromarray(grid).resize(size, Image.Resampling.BICUBIC), dtype=float) / 255


def artwork(directory):
    width, height = size = (960, 540)
    yy, xx = np.mgrid[:height, :width]
    rng = np.random.default_rng(1605)
    haze = np.exp(-((xx - 860)**2 / 240000 + (yy - 40)**2 / 72000))
    sky = np.stack([5 + 16*haze, 10 + 18*haze, 19 + 19*haze], axis=-1)
    stars = Image.fromarray(sky.astype(np.uint8)).convert('RGBA')
    pen = ImageDraw.Draw(stars)
    for _ in range(660):
        x, y = rng.integers([0, 0], [width, height])
        lum = int(rng.integers(43, 182))
        pen.point((int(x), int(y)), (lum, lum, min(lum+16, 255)))
    stars.save(directory / 'Deep space.png')

    # An illuminated sphere with multiscale surface texture, viewed from low orbit.
    radius = 570.
    nx, ny = (xx - 438) / radius, (yy - 770) / radius
    nz = np.sqrt(np.maximum(0, 1 - nx*nx - ny*ny))
    inside = nx*nx + ny*ny <= 1
    illumination = np.maximum(0, nx*.48 - ny*.48 - nz*.68)
    texture = sum(_noise(rng, size, s)*w for s, w in [(56,.48),(17,.29),(4,.16),(2,.07)])
    ribbons = (np.sin(yy*.045 + np.sin(xx*.008)*2 + 13*texture) + 1)*.5
    light = (illumination**.72) * (.54 + .8*texture + .10*ribbons)
    rim = np.exp(-nz*26) * (.45 + .5*(nx+1)/2)
    channels = [4 + 244*light + 160*rim, 9 + 133*light + 103*rim, 17 + 69*light + 57*rim]
    planet = np.stack(channels + [inside*255], axis=-1)
    Image.fromarray(np.uint8(np.clip(planet, 0, 255))).save(directory / 'Copper horizon.png')

    probe = Image.new('RGBA', size)
    pen = ImageDraw.Draw(probe)
    x, y = 622, 153
    pen.line((x-36,y+12,x+48,y-10),fill='#c4d8dc',width=2)
    for dx, dy in [(-36,7),(17,-9)]:
        pen.polygon([(x+dx,y+dy),(x+dx+22,y+dy-5),(x+dx+25,y+dy+3),(x+dx+3,y+dy+8)],fill='#35576a',outline='#adad9e')
        for step in (6,12,18):
            pen.line((x+dx+step,y+dy-step*.22,x+dx+step+3,y+dy+7-step*.22),fill='#658799')
    pen.polygon([(x-8,y-3),(x+3,y-9),(x+12,y-2),(x+4,y+6),(x-6,y+5)],fill='#b08b61',outline='#eedcc4')
    pen.line((x+1,y-7,x-4,y-20),fill='#ddd2bd',width=1)
    probe.save(directory / 'Survey satellite.png')


def create_demo(folder):
    project = Project.create(folder, 'Copper Orbit')
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        artwork(directory)
        layers = []
        for name in ('Deep space', 'Copper horizon', 'Survey satellite'):
            asset = project.add_asset(directory / (name + '.png'))
            layers.append(dict(name=name, asset=asset, x=0, y=0, scale=1, rotation=0, opacity=1, visible=True))
        revisions = []
        composition_id = None
        for dx in (-85, 0, 85):
            layers[-1]['x'] = dx
            revision = project.save_composition('Orbital pass', 960, 540, layers, composition_id=composition_id)
            if composition_id is None:
                composition_id = next(iter(project.state()['compositions']))
            revisions.append(revision)
        shot = project.add_shot('Orbital pass', duration=5)
        project.set_guides(shot, [dict(frame=f, revision=r, role='image') for f, r in zip((0, 60, 119), revisions)])
    return project
