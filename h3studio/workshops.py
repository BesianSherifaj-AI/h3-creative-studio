"""Editable lessons, each in its own reopenable project under the Studio home."""
import base64
import uuid
from functools import lru_cache
from io import BytesIO

from PIL import Image

from .sample import SCENES, STARTER, open_example

# Only keys declared here are accepted as project or resource identifiers.
LESSONS = (
    ("image", "Image", "Grade the headland", "Adjust the copper grade and reposition the title on the layered plate.", 0),
    ("motion", "Motion", "Direct the receiver", "Inspect three saved frame guides and change the camera prompt.", 1),
    ("film", "Film", "Direct a three-shot film", "Review the Tidal Relay board, lock a take and open the selected shot.", 2),
    ("game", "Game", "Play Tidal Relay", "Choose a route through the listening station and revisit a turning point.", 0),
    ("edit", "Edit", "Cut a coastal sequence", "Trim the receiver shot against a continuous stereo score.", 1),
    ("voice", "Voice", "Voice the signal", "Cast a narrator, audition a line and place it in the film mix.", 2),
    ("delivery", "Delivery", "Frame a vertical cut", "Compare Fit and Center fill before exporting the sequence.", 0),
    ("assistant", "AI clients", "Pair an assistant", "Connect a client to this example project and inspect its shots.", 1),
)


@lru_cache(maxsize=3)
def _thumbnail(scene):
    with Image.open(STARTER / (scene + ".jpg")) as plate:
        plate.thumbnail((460, 258), Image.Resampling.LANCZOS)
        data = BytesIO()
        plate.convert("RGB").save(data, "JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(data.getvalue()).decode("ascii")


def catalog():
    return [dict(id=key, room=room, title=title, detail=detail, image=_thumbnail(SCENES[i][0]))
            for key, room, title, detail, i in LESSONS]


def open_lesson(home, key):
    for ident, room, _, _, scene in LESSONS:
        if ident == key:
            project = open_example(home, "Tidal Relay - " + room)
            doc = project.state()
            shot = doc.get("example_shots", {}).get(SCENES[scene][0])
            if shot is None:
                # Earlier editions recorded these IDs on the original starter board.
                board = str(uuid.uuid5(uuid.UUID(doc["id"]), "Tidal Relay example board"))
                rows = doc.get("director", {}).get("boards", {}).get(board, {}).get("rows", [])
                shot = rows[scene]["shot"] if scene < len(rows) else None
            return project, {"lesson": key, "shot": shot}
    raise ValueError("Choose a lesson from the examples library.")
