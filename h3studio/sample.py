"""Open the editable, locally owned Tidal Relay starter project."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from .director import seed_example
from .project import Project

STARTER = Path(__file__).with_name("starter")
WIDTH, HEIGHT = 1600, 900
SCENES = (
    ("headland","The listening station",4,"fl2va",1793,
     "A slow push toward a copper orbital receiver on a storm-beaten headland. Salt spray lifts into the dusk light. Keep the ring anchored to the same stretch of rock."),
    ("receiver","The receiver wakes",3,"i2va",4117,
     "Slide across the analog receiver in the lamp-lit room. A meter needle shifts twice. The sea and copper ring stay softly out of focus through the window."),
    ("breakwater","An answering light",3,"l2va",8573,
     "Drift back from the dark breakwater as a distant glint catches the ring. Waves fold across the basalt foreground. End on the amber opening in the clouds."),
)


def _layers(key, t, assets):
    # The bundled animatic interpolates these transforms between the editable guides.
    if key == "headland":
        scale, x, y = 1+.045*t, -43*t, -16*t
        grade, opacity = "Copper dusk", .55
    elif key == "receiver":
        scale, x, y = 1.045, -8-53*t, -17
        grade, opacity = "Lamplight", .65
    else:
        scale, x, y = 1.045-.045*t, -31*(1-t), -18*(1-t)
        grade, opacity = "Marine blue", .63
    layers = [
        dict(name="Source plate",asset=assets[key+".jpg"],x=round(x),y=round(y),scale=scale),
        dict(name=grade,asset=assets[key+"-grade.png"],opacity=opacity),
    ]
    if key == "headland":
        layers.append(dict(name="Tidal Relay · title",asset=assets["title.png"],x=80,y=708))
    return layers


def _build(project):
    names = ["title.png","listening-line.m4a"]
    for key,*_ in SCENES:
        names.extend((key+".jpg", key+"-grade.png", key+".mp4"))
    assets = {name:project.add_asset(STARTER/name) for name in names}
    shots = []
    for key,title,duration,mode,seed,prompt in SCENES:
        shot = project.add_shot(title,duration)
        shots.append(shot)
        guides, composition = [], None
        frames = duration*24
        for frame in (0,frames//2,frames-1):
            layers = _layers(key,frame/(frames-1),assets)
            rev = project.save_composition(title,WIDTH,HEIGHT,layers,composition)
            composition = project.state()["revisions"][rev]["composition"]
            guides.append(dict(frame=frame,role="image",revision=rev,usage="both"))
        project.set_guides(shot,guides)
        recipe = dict(mode=mode,seed=seed,prompt=prompt,
                      settings={"resolution":"0.5","aspect_ratio":"16:9","quality":"detailed"})
        with project.transaction() as doc:
            doc["compositions"][composition]["current"] = guides[0]["revision"]
            doc["shots"][shot].update(recipe=recipe,director_prompt=prompt)
        # Each standalone take previews its score excerpt. Full-film delivery mixes the single continuous score.
        settings = dict(kind="Studio animatic",width=1280,height=720,frames=frames,fps=24,
                        audio_route=[None],**recipe)
        project.save_take(shot,settings,assets[key+".mp4"])
    with project.transaction() as doc:
        doc["cut_history"] = [[{"shot":ident,"take":doc["shots"][ident]["selected_take"]} for ident in shots]]
        doc.setdefault("soundtracks",{})["listening-line"] = dict(id="listening-line",name="Listening Line",
            asset=assets["listening-line.m4a"],offset=0,gain_db=0,stream=0,track=0)
    seed_example(project,shots)


def open_example(home):
    folder = Path(home).resolve() / "Examples" / "Tidal Relay"
    if folder.exists():
        return Project(folder)
    folder.parent.mkdir(parents=True,exist_ok=True)
    # Publish the fully populated SQLite project and hashed assets together. A competing launch can safely reopen it.
    with TemporaryDirectory(prefix=".tidal-relay-",dir=folder.parent) as temporary:
        staged = Project.create(Path(temporary)/"project","Tidal Relay")
        _build(staged)
        try:
            os.rename(staged.folder,folder)
        except OSError:
            if not folder.is_dir():
                raise
    return Project(folder)
