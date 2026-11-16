"""Phase 1 tests. No network, GPU or model needed:  python -m unittest discover tests -v"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

_TMP = tempfile.TemporaryDirectory()
os.environ["STUDIO_PROJECTS_ROOT"] = _TMP.name
os.environ["STUDIO_CONFIG"] = str(Path(_TMP.name) / "none.yaml")  # ignore the machine's real config

from fastapi.testclient import TestClient  # noqa: E402

from studio_core import api, config, ingest, projects, router, vision  # noqa: E402
from studio_core.projects import StudioError  # noqa: E402
from studio_core.providers import ModelInfo, ProviderError  # noqa: E402

GOOD = {"subject": "two-storey house", "image_type": "exterior_photo", "building_type": "residence",
        "styles": [{"name": "modern", "confidence": "medium"}], "storeys_visible": 2,
        "massing": "two stacked boxes", "roof_form": "flat", "materials":
        [{"name": "concrete", "location": "walls", "confidence": "high"}], "openings": "large glazing",
        "features": ["cantilever"], "landscape": "lawn", "camera_view": "eye level, front",
        "dimensions": [{"what": "facade width", "value_m": 12, "provenance": "measured", "basis": "door"},
                       {"what": "depth", "value_m": None, "provenance": "estimated"}],
        "uncertainties": ["rear elevation"], "overall_confidence": "medium"}


class FakeProvider:
    name = "fake"

    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0

    def chat(self, model, prompt, images=(), schema=None, system="", unload=False, **kw):
        self.calls += 1
        return self.replies.pop(0)


def picture(path, seed=0, size=(640, 480)):
    im = Image.new("RGB", size, (200, 210, 220))
    d = ImageDraw.Draw(im)
    d.rectangle([100 + seed * 90, 150, 400, 400], fill=(120 + seed * 40, 100, 90))
    d.polygon([(80, 150), (250 + seed * 60, 40), (420, 150)], fill=(60, 60, 70 + seed * 50))
    im.save(path)
    return str(path)


class Base(unittest.TestCase):
    n = 0

    def setUp(self):
        Base.n += 1
        self.slug = projects.create_project(f"Test House {Base.n}")["slug"]
        self.dir = projects.project_dir(self.slug)
        self.work = Path(tempfile.mkdtemp(dir=_TMP.name))


class Projects(Base):
    def test_create_has_all_state_files(self):
        for f in projects.STATE_FILES:
            self.assertTrue((self.dir / f).exists(), f)
        self.assertTrue(projects.validate(self.slug)["valid"])

    def test_duplicate_and_traversal_rejected(self):
        with self.assertRaises(StudioError):
            projects.create_project(f"Test House {Base.n}")
        for bad in ("../x", "..", "a/b", "C:\\x", ""):
            with self.assertRaises(StudioError):
                projects.project_dir(bad)

    def test_write_backs_up_and_bumps_revision(self):
        spec = projects.read_state(self.slug, "model_spec.json")
        spec["levels"] = [{"name": "ground", "elevation_m": projects.dim(0.0, "provided")}]
        rev = projects.write_state(self.slug, "model_spec.json", spec, "tester", "add level")
        self.assertEqual(rev, 2)
        self.assertTrue((self.dir / "backups" / "r0001_model_spec.json").exists())
        self.assertEqual(json.loads((self.dir / "backups" / "r0001_model_spec.json").read_text())["levels"], [])
        self.assertEqual(projects.read_state(self.slug, "revision_history.json")["revisions"][-1]["author"], "tester")
        with self.assertRaises(StudioError):
            projects.write_state(self.slug, "project.json", {})

    def test_lock_blocks_second_writer(self):
        with projects.lock(self.slug):
            with self.assertRaises(StudioError):
                with projects.lock(self.slug, wait_s=0.3):
                    pass
        with projects.lock(self.slug, wait_s=0.3):
            pass  # released

    def test_validate_catches_bad_provenance(self):
        spec = projects.read_state(self.slug, "model_spec.json")
        spec["levels"] = [{"h": {"value": 3.0, "provenance": "guessed"}},
                          {"h": {"value": 3.0, "provenance": "unknown"}},
                          {"h": {"value": None, "provenance": "measured"}}]
        projects.write_state(self.slug, "model_spec.json", spec)
        self.assertEqual(len(projects.validate(self.slug)["problems"]), 3)

    def test_decisions_default_to_proposed(self):
        d = projects.add_decision(self.slug, "Flat roof", "from REF001", "agent")
        self.assertEqual((d["id"], d["status"]), ("D001", "proposed"))


class Ingest(Base):
    def test_import_thumbnail_and_duplicates(self):
        a = picture(self.work / "a.png", 0)
        r = ingest.import_reference(self.slug, a)["added"][0]
        self.assertEqual(r["id"], "REF001")
        self.assertTrue((self.dir / r["original"]).exists() and (self.dir / r["thumbnail"]).exists())
        self.assertEqual(Path(a).read_bytes(), (self.dir / r["original"]).read_bytes())  # original untouched
        # exact duplicate: skipped
        self.assertEqual(ingest.import_reference(self.slug, a)["added"], [])
        # same picture re-encoded smaller: kept but flagged
        Image.open(a).resize((320, 240)).save(self.work / "a_small.jpg", quality=70)
        near = ingest.import_reference(self.slug, str(self.work / "a_small.jpg"))["added"][0]
        self.assertEqual(near.get("near_duplicate_of"), "REF001")
        # different picture: not flagged
        other = ingest.import_reference(self.slug, picture(self.work / "b.png", 2))["added"][0]
        self.assertNotIn("near_duplicate_of", other)
        self.assertTrue(projects.validate(self.slug)["valid"])

    def test_rejects_unsupported_and_corrupt(self):
        (self.work / "x.exe").write_bytes(b"MZ")
        (self.work / "bad.png").write_bytes(b"not an image")
        for f in ("x.exe", "bad.png", "missing.png"):
            with self.assertRaises(StudioError):
                ingest.import_reference(self.slug, str(self.work / f))
        self.assertEqual(projects.read_state(self.slug, "reference_manifest.json")["references"], [])
        self.assertEqual(list((self.dir / "references" / "originals").iterdir()), [])


class Vision(Base):
    def setUp(self):
        super().setUp()
        ingest.import_reference(self.slug, picture(self.work / "a.png"))

    def test_retry_then_valid_and_provenance_downgraded(self):
        fake = FakeProvider(["A two-storey house.", "sorry, here you go: {\"subject\": 1}",
                             "```json\n" + json.dumps(GOOD) + "\n```"])
        rec = vision.analyse(self.slug, "REF001", provider=fake, model="m")
        self.assertEqual((fake.calls, rec["validation_retries"]), (3, 1))
        self.assertEqual(rec["description"], "A two-storey house.")
        dims = rec["analysis"]["dimensions"]
        self.assertEqual(dims[0]["provenance"], "estimated")   # model claimed "measured" from a photo
        self.assertEqual(dims[1]["provenance"], "unknown")     # no value -> unknown
        self.assertTrue((self.dir / "analysis" / "REF001.json").exists())
        self.assertIn("REF001", (self.dir / "analysis" / "reference_analysis.md").read_text(encoding="utf-8"))
        self.assertTrue(projects.validate(self.slug)["valid"])

    def test_analysis_never_touches_brief_or_spec(self):
        before = [(self.dir / f).read_bytes() for f in ("design_brief.yaml", "model_spec.json", "decisions.json")]
        vision.analyse(self.slug, "REF001", provider=FakeProvider(["desc", json.dumps(GOOD)]), model="m")
        self.assertEqual(before, [(self.dir / f).read_bytes() for f in
                                  ("design_brief.yaml", "model_spec.json", "decisions.json")])

    def test_batch_survives_provider_failure(self):
        class Boom(FakeProvider):
            def chat(self, *a, **k):
                raise ProviderError("timed out")
        orig = vision.analyse
        vision.analyse = lambda slug, rid, **k: orig(slug, rid, provider=Boom([]), model="m")
        try:
            out = vision.analyse_all(self.slug)
        finally:
            vision.analyse = orig
        self.assertEqual((out["analysed"], out["failed"][0]["reference"]), ([], "REF001"))

    def test_gives_up_after_retries(self):
        with self.assertRaises(StudioError):
            vision.analyse(self.slug, "REF001", provider=FakeProvider(["desc", "nope", "{}"]), model="m")
        self.assertFalse((self.dir / "analysis" / "REF001.json").exists())


class Router(unittest.TestCase):
    models = [ModelInfo("ollama", "llama3.2-vision:latest", {"completion", "vision", "json"}, True, 7800),
              ModelInfo("ollama", "llava:latest", {"completion", "vision", "json"}, True, 4700),
              ModelInfo("ollama", "gemma4:31b-cloud", {"completion", "vision", "json"}, False, 0),
              ModelInfo("ollama", "qwen2.5:7b", {"completion", "tools", "json"}, True, 4700),
              ModelInfo("anthropic", "claude-opus-5-5", {"completion", "vision", "json"}, False, 0)]

    def pick(self, task, vram, cloud=False, prefs=None):
        cfg = config.load()
        cfg["privacy"]["allow_cloud"] = cloud
        if prefs is not None:
            cfg["routing"][task] = prefs
        return router.pick(task, self.models, cfg, vram)

    def test_prefs_win_then_vram_fit(self):
        c = self.pick("vision", 7900)  # first in prefs even though it spills past 8 GB
        self.assertEqual(c["model"].name, "llama3.2-vision:latest")
        self.assertIn("spill", c["reason"])
        self.assertEqual(self.pick("vision", 7900, prefs=[])["model"].name, "llava:latest")
        self.assertEqual(self.pick("vision", 24000, prefs=[])["model"].name, "llama3.2-vision:latest")

    def test_cloud_blocked_unless_allowed(self):
        c = self.pick("reasoning", 7900)
        self.assertTrue(c["model"].local)
        self.assertEqual(len(c["rejected"]), 2)  # anthropic + the ollama "-cloud" proxy model
        self.assertEqual(self.pick("reasoning", 7900, cloud=True)["model"].id, "anthropic/claude-opus-5-5")

    def test_capability_required(self):
        self.assertNotIn("vision", self.pick("json", 7900)["model"].capabilities - {"completion", "json", "tools"})
        with self.assertRaises(ProviderError):
            self.pick("embedding", 7900)


class Api(unittest.TestCase):
    def test_end_to_end_without_model(self):
        c = TestClient(api.app)
        self.assertEqual(c.get("/health").json(), {"status": "ok"})
        slug = c.post("/projects", json={"name": "API House"}).json()["slug"]
        self.assertEqual(c.post("/projects", json={"name": "API House"}).status_code, 409)
        self.assertEqual(c.get("/projects/nope/state/project.json").status_code, 404)
        self.assertEqual(c.get(f"/projects/{slug}/state/secrets.txt").status_code, 400)
        img = picture(Path(_TMP.name) / "api.png", 1)
        r = c.post(f"/projects/{slug}/references", json={"path": img})
        self.assertEqual(r.json()["added"][0]["id"], "REF001")
        self.assertEqual(c.post(f"/projects/{slug}/decisions", json={"title": "x"}).status_code, 201)
        self.assertTrue(c.post(f"/projects/{slug}/validate").json()["valid"])
        self.assertIn(slug, [p["slug"] for p in c.get("/projects").json()["projects"]])

    def test_ui_and_files(self):
        c = TestClient(api.app)
        r = c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers["content-type"])
        slug = c.post("/projects", json={"name": "UI House"}).json()["slug"]
        img = picture(Path(_TMP.name) / "ui.png", 2)
        ref = c.post(f"/projects/{slug}/references", json={"path": img}).json()["added"][0]
        r = c.get(f"/projects/{slug}/files/{ref['thumbnail']}")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("image/"))
        for bad in ("project.json", "backups/x", "../../etc", "references/../design_brief.yaml"):
            self.assertEqual(c.get(f"/projects/{slug}/files/{bad}").status_code, 404, bad)


if __name__ == "__main__":
    unittest.main()
