"""Plan-image and 3D-model importers. The image test uses fake models; the mesh test needs real Blender."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

_TMP = tempfile.TemporaryDirectory()
_ROOT = Path(__file__).resolve().parents[1]
os.environ["STUDIO_PROJECTS_ROOT"] = _TMP.name
_real = _ROOT / "config" / "studio.yaml"
_exe = (yaml.safe_load(_real.read_text(encoding="utf-8")) or {}).get("blender", {}).get("executable") \
    if _real.exists() else None
_cfg = Path(_TMP.name) / "studio_test.yaml"
_cfg.write_text(yaml.safe_dump({"blender": {"executable": _exe}} if _exe else {}), encoding="utf-8")
os.environ["STUDIO_CONFIG"] = str(_cfg)

from PIL import Image, ImageDraw  # noqa: E402

from studio_core import imports, projects  # noqa: E402
from studio_core.importers import plan_image  # noqa: E402

HAS_BLENDER = bool(_exe) and Path(_exe).is_file()


class Fake:
    name = "fake"

    def __init__(self, replies):
        self.replies = list(replies)

    def chat(self, model, prompt, images=(), schema=None, system="", unload=False, **kw):
        return self.replies.pop(0)


JSON = ('{"rooms":[{"name":"Living","written_dims":"12\'0\\" x 15\'6\\"","width_m":0,"length_m":0},'
        '{"name":"Master Bedroom","written_dims":"","width_m":0,"length_m":0}],'
        '"overall_width_m":null,"overall_length_m":null}')


def draw_plan(path):
    im = Image.new("RGB", (800, 600), "white")
    d = ImageDraw.Draw(im)
    d.rectangle([100, 100, 700, 500], outline="black", width=10)      # 600 x 400 px outer walls
    d.line([(400, 100), (400, 500)], fill="black", width=10)          # interior wall
    for y in range(130, 480, 14):                                     # text-like thin strokes
        d.line([(430, y), (520, y)], fill="black", width=1)
    d.line([(100, 560), (700, 560)], fill="black", width=1)           # dimension line
    d.line([(100, 550), (100, 570)], fill="black", width=1)
    d.line([(700, 550), (700, 570)], fill="black", width=1)
    im.save(path)


class Parser(unittest.TestCase):
    def test_lengths(self):
        for text, want in [("12'6\"", 12.5 * 0.3048), ("12' 6\"", 12.5 * 0.3048), ("12'-6\"", 12.5 * 0.3048),
                           ("12'", 12 * 0.3048), ("3.6m", 3.6), ("3600mm", 3.6), ("3.6", 3.6), ("3,6 m", 3.6)]:
            self.assertAlmostEqual(plan_image.parse_length(text), want, places=3, msg=text)
        self.assertIsNone(plan_image.parse_length("big"))

    def test_dims(self):
        w, l = plan_image.parse_dims("12'0\" x 10'6\"")
        self.assertAlmostEqual(w, 3.6576, places=3)
        self.assertAlmostEqual(l, 3.2004, places=3)
        self.assertEqual(plan_image.parse_dims("3.6 x 3.0"), (3.6, 3.0))
        self.assertEqual(plan_image.parse_dims("3600mm X 3000mm"), (3.6, 3.0))
        self.assertIsNone(plan_image.parse_dims("12'0\""))
        self.assertIsNone(plan_image.parse_dims(""))

    def test_room_type(self):
        self.assertEqual(plan_image.room_type("Master Bedroom"), "master_bedroom")
        self.assertEqual(plan_image.room_type("Toilet"), "wc")
        self.assertEqual(plan_image.room_type("Widget"), "other")


class PlanImage(unittest.TestCase):
    def setUp(self):
        self.img = Path(_TMP.name) / "plan.png"
        draw_plan(self.img)

    def go(self, **kw):
        p = Fake(["Living 12'0\" x 15'6\"\nMaster Bedroom (no dimensions)", JSON])
        return plan_image.run(self.img, provider=p, model="m", **kw)

    def test_scaled(self):
        geo, notes = self.go(width_m=12)
        self.assertGreaterEqual(len(geo.walls), 4)
        xs = [p[0] for w in geo.walls for p in (w.start, w.end)]
        ys = [p[1] for w in geo.walls for p in (w.start, w.end)]
        self.assertAlmostEqual(max(xs) - min(xs), 12, delta=0.6)
        self.assertAlmostEqual(max(ys) - min(ys), 8, delta=0.4)
        self.assertEqual(min(xs), 0)
        self.assertEqual(min(ys), 0)
        self.assertTrue(all(w.provenance == "measured" for w in geo.walls))
        self.assertTrue(all(abs(w.thickness_m - 0.2) <= 0.08 for w in geo.walls), [w.thickness_m for w in geo.walls])
        self.assertEqual(len(geo.walls), 5, "4 outer + 1 interior; text and dimension lines must be ignored")
        self.assertEqual(sum(w.kind == "interior" for w in geo.walls), 1)
        living, bed = geo.spaces
        self.assertAlmostEqual(living.area_m2, 3.6576 * 4.7244, places=2)
        self.assertEqual(living.provenance, "provided")
        self.assertEqual(living.type, "living")
        self.assertIsNone(bed.area_m2)
        self.assertEqual(bed.provenance, "unknown")
        self.assertEqual(bed.type, "master_bedroom")
        self.assertTrue(any("not extracted" in n for n in notes))

    def test_unscaled_has_no_walls(self):
        geo, notes = self.go()
        self.assertEqual(geo.walls, [])
        self.assertEqual(len(geo.spaces), 2)
        self.assertTrue(any("give width_m" in n for n in notes))

    def test_width_written_on_drawing(self):
        p = Fake(["x", JSON.replace('"overall_width_m"', '"overall_written":"40\'0\\"","overall_width_m"')])
        geo, notes = plan_image.run(self.img, provider=p, model="m")
        xs = [q[0] for w in geo.walls for q in (w.start, w.end)]
        self.assertAlmostEqual(max(xs), 40 * 0.3048, delta=0.3)
        self.assertTrue(any("written on the drawing" in n for n in notes))


@unittest.skipUnless(HAS_BLENDER, "no Blender executable in config/studio.yaml")
class Mesh(unittest.TestCase):
    def setUp(self):  # other test modules overwrite STUDIO_CONFIG at import; pin Blender per test
        p = mock.patch.dict(os.environ, {"BLENDER_EXE": _exe, "STUDIO_PROJECTS_ROOT": _TMP.name})
        p.start()
        self.addCleanup(p.stop)

    def test_glb_import_and_approve(self):
        out = Path(_TMP.name) / "glb"
        subprocess.run([_exe, "--background", "--python", str(_ROOT / "engines" / "blender" / "massing_preview.py"),
                        "--", str(out), "12", "8", "2"], check=True, capture_output=True, timeout=600)
        glb = out / "massing_preview.glb"
        self.assertTrue(glb.exists())
        slug = projects.create_project("Mesh Import")["slug"]
        rec = imports.import_file(slug, str(glb))
        g = rec["geometry"]
        self.assertGreaterEqual(len(g["levels"]), 1)
        self.assertGreaterEqual(len(g["walls"]), 3)
        self.assertTrue(all(w["provenance"] == "measured" for w in g["walls"]))
        self.assertTrue(Path(g["source"]["preview"]).exists())
        # 28 x 24 m ground plane is excluded; hull = 12 x 8 box + 0.5 m roof overhang each side
        self.assertAlmostEqual(rec["summary"]["extent_m"][0], 13, delta=0.3)
        self.assertAlmostEqual(rec["summary"]["extent_m"][1], 9, delta=0.3)
        self.assertTrue(any("SITE_GROUND" in n for n in rec["notes"]))
        imports.approve(slug, rec["id"])
        self.assertTrue(projects.validate(slug)["valid"])


if __name__ == "__main__":
    unittest.main()


class DescriptionFallback(unittest.TestCase):
    """Live bug: the vision model read "Living Room: 14'0" x 17'0"" but the JSON step returned
    written_dims="". The size must then be recovered from the description, in code."""

    def test_find_dims_in_sentences(self):
        from studio_core.importers.plan_image import find_dims
        self.assertEqual(find_dims("*   Living Room: 14'0\" x 17'0\""), (4.2672, 5.1816))
        self.assertEqual(find_dims("kitchen 3.6 x 3.0 m"), (3.6, 3.0))
        self.assertIsNone(find_dims("Total area 1200 sq ft"))
