import hashlib
import json
import math
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

from h3studio.project import Project, packet_info


def motion_packet(path, frames=124, corrupt=False, seed=123):
    shapes = {"video":[1,24,2+5*((frames-5)//17),4,8],"audio":[1,32,2,round(frames*40/24)]}
    header = {}
    offset = 0
    for name,shape in shapes.items():
        end = offset + math.prod(shape)*2
        header[name] = {"dtype":"F16","shape":shape,"data_offsets":[offset,end]}
        offset = end
    encoded = json.dumps(header).encode()
    encoded += b" " * (-len(encoded)%8)
    content = len(encoded).to_bytes(8,"little")+encoded+bytes(offset)
    latent = {"id":"resource-1","role":"auxiliary","kind":"latent","path":"streams/latent.bin",
              "content":{"revision":2,"digest":"sha256:"+hashlib.sha256(content).hexdigest()},
              "extensions":{"minimax_h3":{"latent":{
                  "contract_version":2,"origin":"sampler_output","model_family":"minimax_h3","layout":"joint_av",
                  "streams":{k:{"shape":v} for k,v in shapes.items()},
                  "batch":1,"binding":{"time":"bound","geometry":"bound"},
                  "timeline":{"frames":frames,"fps":24,"temporal_grid":"h3_causal_17k5","absolute_start_frame":0},
                  "canvas":{"width":128,"height":64}}}}}
    with zipfile.ZipFile(path,"w") as archive:
        archive.writestr("packet.json",json.dumps({"id":"packet-1","primary":{"latent":"resource-1"},"resources":[latent],"generation":{"seed":seed}}))
        archive.writestr("streams/latent.bin",content[:-1]+b"X" if corrupt else content)
    return path


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = Project.create(self.root/"project","Test")

    def image(self,name,color,size=(8,8)):
        path = self.root/name
        Image.new("RGBA",size,color).save(path)
        return self.project.add_asset(path)

    def test_immutable_layer_edits_and_oversize_canvas(self):
        base = self.image("a.png","red")
        overlay = self.image("b.png","blue",(2,2))
        mask = Image.new("L",(2,2),0)
        mask.putpixel((0,0),255)
        mask.save(self.root/"mask.png")
        layer = {"asset":overlay,"x":3,"y":4,"opacity":.5,"mask":self.project.add_asset(self.root/"mask.png")}
        rev = self.project.save_composition("Wide",4200,8,[{"asset":base},layer])
        comp = self.project.state()["revisions"][rev]["composition"]
        edited = self.project.save_composition("Wide",4200,8,[{"asset":base},dict(layer,visible=False)],comp)
        self.assertEqual(self.project.render_revision(rev).getpixel((3,4)),(127,0,128,255))
        self.assertEqual(self.project.render_revision(rev).getpixel((4,4)),(255,0,0,255))
        self.assertEqual(self.project.render_revision(edited).getpixel((3,4)),(255,0,0,255))
        with self.assertRaisesRegex(ValueError,"changed"):
            self.project.save_composition("Race",10,10,[],comp,base_revision=rev)

    def test_branches_use_selected_parent_and_preserve_old_cut(self):
        a = self.image("a.png","red")
        r = self.project.save_composition("A",8,8,[{"asset":a}])
        sh = self.project.add_shot("First",5)
        guides = [{"frame":i,"revision":r,"role":"guide"} for i in (0,60,119)]
        self.project.set_guides(sh,guides)
        packet = self.project.add_asset(motion_packet(self.root/"video.mmh3"))
        settings = {"seed":123,"frames":124,"loras":[{"name":"b"},{"name":"a"}],"trim":{"end_frame":119}}
        t = self.project.save_take(sh,settings,packet_asset=packet)
        child = self.project.branch_take(t,"continue")
        self.assertEqual(self.project.state()["shots"][child]["start_mode"],"choose_ending")
        self.project.choose_start(child,"visual",revision=r)
        self.assertEqual(self.project.state()["shots"][child]["start_revision"],r)
        self.assertEqual(self.project.state()["shots"][child]["guides"],[])
        self.project.set_guides(sh,[guides[0]])
        reroll = self.project.save_take(sh,dict(settings,seed=100))
        self.assertTrue(self.project.state()["shots"][child]["needs_review"])
        self.assertEqual(self.project.state()["takes"][t]["guides"],guides)
        retry = self.project.branch_take(t,"retry")
        self.assertEqual(self.project.state()["shots"][retry]["settings"]["seed"],123)
        scene = self.project.branch_take(reroll,"scene")
        self.assertNotIn("parent_take",self.project.state()["shots"][scene])
        index = next(i for i,cut in enumerate(self.project.state()["cut_history"]) if cut == [{"shot":sh,"take":t}])
        self.project.restore_cut(index)
        self.assertEqual(self.project.state()["cut"],[sh])
        self.assertEqual(self.project.state()["shots"][sh]["selected_take"],t)

    def test_retry_replacement_reviews_downstream_and_restoring_reconciles(self):
        p = self.project
        a = p.add_shot("A",5)
        take_a = p.save_take(a,{"seed":1})
        b = p.branch_take(take_a,"continue")
        take_b = p.save_take(b,{"seed":2})
        c = p.branch_take(take_b,"continue")
        take_c = p.save_take(c,{"seed":3})
        d = p.branch_take(take_c,"scene")
        take_d = p.save_take(d,{"seed":4})
        original = [{"shot":s,"take":t} for s,t in [(a,take_a),(b,take_b),(c,take_c),(d,take_d)]]
        alternative = p.branch_take(take_a,"retry")
        alt_take = p.save_take(alternative,{"seed":5})
        replacement = [{"shot":alternative,"take":alt_take},*original[1:]]
        reopened = Project(p.folder)
        def check(review):
            doc = Project(p.folder).state()
            self.assertEqual([doc["shots"][s].get("needs_review",False) for s in (b,c,d)], [review,review,False])
            self.assertEqual(doc["shots"][b]["parent_take"],take_a)
            self.assertEqual(doc["takes"][take_b]["parent_take"],take_a)
            self.assertEqual(doc["takes"][take_c]["parent_take"],take_b)
            self.assertEqual(doc["shots"][alternative]["parent_take"],take_a)
        check(True)
        reopened.restore_cut(reopened.state()["cut_history"].index(original))
        check(False)
        reopened.restore_cut(reopened.state()["cut_history"].index(replacement))
        check(True)

    def test_switching_legacy_injected_start_preserves_authored_cues(self):
        p = self.project
        a = self.image("paint.png","orange")
        r = p.save_composition("Last frame",8,8,[{"asset":a}])
        shot = p.add_shot("A",5)
        packet = p.add_asset(motion_packet(self.root/"video.mmh3"))
        t = p.save_take(shot,{"seed":123},video_asset=a,packet_asset=packet)
        child = p.branch_take(t,"continue")
        authored = [{"frame":0,"role":"image","revision":r,"usage":"both"},
                    {"frame":0,"role":"video","asset":a},{"frame":0,"role":"audio","asset":a}]
        with p.transaction() as state:
            state["shots"][child].update(start_mode="visual",start_revision=r,
                                        guides=[{"frame":0,"role":"image","revision":r},*authored])
        Project(p.folder).choose_start(child,"original_motion")
        self.assertEqual(Project(p.folder).state()["shots"][child]["guides"],authored)

    def test_portability_and_conflicting_filenames(self):
        a = self.image("source.png","red")
        b = self.image("source.png","blue")
        self.assertNotEqual(a,b)
        copy = self.root/"moved"
        self.project.folder.rename(copy)
        (self.root/"source.png").unlink()
        reopened = Project(copy)
        for ident in (a,b):
            self.assertEqual(hashlib.sha256(reopened.asset_path(ident).read_bytes()).hexdigest(),ident)

    def test_packet_validation(self):
        path = motion_packet(self.root/"a.mmh3")
        self.assertEqual(packet_info(path,{"frames":124,"seed":123})["revision"],2)
        with self.assertRaisesRegex(ValueError,"geometry"):
            packet_info(path,{"frames":175})
        with self.assertRaisesRegex(ValueError,"seed"):
            packet_info(path,{"seed":122})
        with self.assertRaisesRegex(ValueError,"checksum"):
            packet_info(motion_packet(path,corrupt=True))

    def test_take_trim_uses_exclusive_delivery_end_and_preserves_exact_source_seconds(self):
        trim = Project._take_trim
        self.assertEqual(trim({"render_trim":[39,159]}),{"start_frame":39,"end_frame":158})
        self.assertEqual(trim({"render_trim":[0,120]}),{"start_frame":0,"end_frame":119})
        seconds=[39/24,141.5/24]
        self.assertEqual(trim({"render_trim":[39,159],"delivery_trim":seconds}),
                         {"start_frame":39,"end_frame":141,"seconds":seconds})

    def test_simultaneous_writers(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(lambda i:Project(self.project.folder).add_shot(str(i)),range(12)))
        self.assertEqual(set(self.project.state()["cut"]),set(ids))

    def test_job_adoption_is_atomic_across_open_instances(self):
        asset = self.image("frame.png","blue")
        shot = self.project.add_shot("Opening",5)
        def adopt(index):
            p = Project(self.project.folder)
            revision = p.save_composition("AI still",8,8,[{"asset":asset}],job_id="image-job")
            take = p.save_take(shot,{"job_id":"video-job","seed":123})
            return revision,take
        with ThreadPoolExecutor(max_workers=4) as pool:
            all_results = list(pool.map(adopt,range(8)))
        self.assertEqual(len(set(all_results)),1)
        state = self.project.state()
        self.assertEqual(len(state["takes"]),1)
        self.assertEqual(len(state["revisions"]),1)
