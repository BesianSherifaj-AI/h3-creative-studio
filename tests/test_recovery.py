import hashlib
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image

from h3studio.project import Project
from h3studio.recovery import import_legacy, relink
from test_project import motion_packet


def doc(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.studio = Project.create(self.root/"Creative","Film")
        self.data = self.root/"Legacy"
        self.data.mkdir()

    def atelier(self):
        (self.data/"assets").mkdir()
        with closing(sqlite3.connect(self.data/"studio.sqlite")) as db, db:
            db.executescript("""
            CREATE TABLE assets(id TEXT,name TEXT,sha256 TEXT);
            CREATE TABLE projects(id TEXT,name TEXT,payload TEXT,updated REAL);
            CREATE TABLE project_revisions(id TEXT,project TEXT,name TEXT,payload TEXT,created REAL);
            """)
            for i,color in enumerate(("red","blue")):
                asset = f"asset-{i}"
                file = self.data/"assets"/(asset+".png")
                Image.new("RGB",(32,32),color).save(file)
                sha = hashlib.sha256(file.read_bytes()).hexdigest()
                db.execute("INSERT INTO assets VALUES (?,?,?)",(asset,"same filename",sha))
                payload = json.dumps({"width":32,"height":32,"layers":[{"asset":asset}]})
                db.execute("INSERT INTO project_revisions VALUES (?,?,?,?,?)",(str(i),"p","Art",payload,i+1))
            db.execute("INSERT INTO projects VALUES (?,?,?,?)",("p","Art",payload,2))

    def test_installed_history_interruption_repeat_move(self):
        self.atelier()
        real = self.studio.store_stream
        def interrupted(stream,name,expected_hash):
            if name == "same filename.png" and any((self.studio.folder/"assets").glob("*/*.png")):
                raise RuntimeError("interrupted")
            return real(stream,name,expected_hash)
        with patch.object(self.studio,"store_stream",side_effect=interrupted),self.assertRaises(RuntimeError):
            import_legacy(self.studio,self.data)
        self.assertFalse(self.studio.state()["compositions"])
        report = import_legacy(self.studio,self.data)
        self.assertEqual((report["revisions"],report["assets"]),(2,2))
        self.assertEqual(report,import_legacy(self.studio,self.data/"studio.sqlite"))
        shutil.rmtree(self.data)
        new = self.root/"Moved"; self.studio.folder.rename(new)
        moved = Project(new)
        revs = next(iter(moved.state()["compositions"].values()))["revisions"]
        self.assertEqual([moved.render_revision(r).getpixel((0,0)) for r in revs],[(255,0,0,255),(0,0,255,255)])

    def prompt(self):
        p = {"id":"project","title":"Movie","workspace":"game","story_session_id":"story",
             "assets":[],"shots":[{"id":"shot","action":"Move","duration":5}]}
        doc(self.data/"projects/project.json",p)
        doc(self.data/"history/project/1.json",dict(p,title="First version"))
        doc(self.data/"stories/story.json",{"id":"story","mode":"game","active_branch_id":"branch",
             "branches":{"branch":["a","b"],"alternate":["b"]},"turns":[]})
        doc(self.data/"films/film.json",{"id":"film","title":"Film","latest_batch_id":"batch"})
        doc(self.data/"production/batch/record.json",{"id":"batch","items":[{"run_id":"a"}]})
        media = self.root/"ComfyOutput"
        for index,run in enumerate(("a","b")):
            folder=self.data/"video_runs"/run
            rec = {"id":run,"project_id":"project","width":128,"height":64,"seed":123,"frames":124,"duration":5,
                   "continuation_source":f"{run}.mmh3","status":"succeeded","created_at":index,"parent_run_id":"a" if index else None,
                   "video":{"filename":run+".mp4"}}
            doc(folder/"record.json",rec)
            doc(folder/"project.json",p)
            doc(folder/"transfer.json",{"manifest":{"loras":[{"name":"first"},{"name":"second"}]},"prompt":{"1":{}}})
            (folder/"playback.mp4").write_bytes(b"test video identity "+run.encode())
            media.mkdir(exist_ok=True)
            motion_packet(media/(run+".mmh3"))
        return p,media

    def test_prompt_film_game_and_motion(self):
        p,media = self.prompt()
        report = import_legacy(self.studio,self.data,media)
        self.assertEqual(report["missing"],[])
        self.assertEqual(report["takes"],2)
        self.assertEqual(report,import_legacy(self.studio,self.data,media))
        self.studio.open_legacy_cut("story:story:branch")
        state = self.studio.state()
        selected = [state["takes"][state["shots"][sh]["selected_take"]] for sh in state["cut"]]
        self.assertEqual([t["legacy_run"] for t in selected],["a","b"])
        self.assertEqual(selected[1]["parent_take"],selected[0]["id"])
        self.assertEqual(selected[1]["packet"]["revision"],2)
        self.assertEqual(selected[1]["settings"]["loras"],[{"name":"first"},{"name":"second"}])
        self.assertIn("history/project/1.json",state["legacy"]["documents"])
        self.assertIn("films/film.json",state["legacy"]["documents"])
        self.assertEqual(state["shots"][state["cut"][0]]["workspace"],"game")
        shutil.rmtree(self.data); shutil.rmtree(media)
        child = self.studio.branch_take(selected[1]["id"],"continue")
        self.studio.choose_start(child,"original_motion")
        self.assertTrue(self.studio.asset_path(selected[1]["packet_asset"]).is_file())

    def test_zip_missing_relink_checks_bytes(self):
        media = self.data/"clip.png"
        Image.new("RGB",(9,9),"green").save(media)
        p = {"id":"p","title":"Game","assets":[{"id":"asset","filename":"clip.png","sha256":hashlib.sha256(media.read_bytes()).hexdigest()}],
             "story_session_id":"s","shots":[{"id":"s1","duration":4}]}
        package = self.root/"p.zip"
        with zipfile.ZipFile(package,"w") as z:z.writestr("project.json",json.dumps(p))
        result = import_legacy(self.studio,package)
        missing = {r["kind"]:r["id"] for r in result["missing"]}
        wrong = self.root/"wrong.png"; wrong.write_bytes(b"x")
        with self.assertRaisesRegex(ValueError,"checksum"):
            relink(self.studio,missing["asset"],wrong)
        relink(self.studio,missing["asset"],media)
        story = self.root/"s.json"; doc(story,{"id":"s","branches":{},"turns":[]})
        self.assertEqual(relink(self.studio,missing["story"],story)["missing"],[])
        self.assertEqual(import_legacy(self.studio,package)["missing"],[])

    def test_unsafe_legacy_path_rolls_back(self):
        doc(self.data/"projects/project.json",{"id":"p","assets":[{"id":"../outside"}],"shots":[]})
        with self.assertRaisesRegex(ValueError,"relative path"):
            import_legacy(self.studio,self.data)
        self.assertEqual(self.studio.state()["legacy"],{})

    def test_cancelled_import_restarts_from_intact_state(self):
        self.atelier()
        checks=0
        def cancel():
            nonlocal checks
            checks+=1
            return checks>=4
        with self.assertRaises(InterruptedError):
            import_legacy(self.studio,self.data,cancelled=cancel)
        self.assertFalse(self.studio.state()["revisions"])
        self.assertFalse(list((self.studio.folder/"assets").glob("*.partial")))
        self.assertEqual(import_legacy(self.studio,self.data)["revisions"],2)

    def test_damaged_motion_can_be_relinked_and_survives_repeat_import(self):
        _,media=self.prompt()
        bad=motion_packet(media/"a.mmh3",corrupt=True)
        report=import_legacy(self.studio,self.data,media)
        item=next(x for x in report["missing"] if x["kind"]=="packet")
        self.assertTrue(report["notices"])
        with self.assertRaisesRegex(ValueError,"checksum"):
            relink(self.studio,item["id"],bad)
        valid=motion_packet(self.root/"exact.mmh3")
        self.assertFalse(relink(self.studio,item["id"],valid)["missing"])
        self.assertFalse(import_legacy(self.studio,self.data,media)["missing"])
        takes=self.studio.state()["takes"]
        take=next(t for t in takes.values() if t["legacy_run"]=="a")
        self.assertEqual(take["packet_asset"],hashlib.sha256(valid.read_bytes()).hexdigest())
        self.assertEqual(take["packet"]["revision"],2)
