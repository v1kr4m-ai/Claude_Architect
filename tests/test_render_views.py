"""Blender headless presentation renders: builds a small model, then renders once (skipped without Blender)."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from tests.test_build_model import ROOT, blender_exe, run as build_run, spec

SCRIPT = ROOT / "engines" / "blender" / "render_views.py"
W, H = 480, 270


def render(exe, blend, out, job):
    jp = Path(out).parent / "job.json"
    jp.write_text(json.dumps(job))
    return subprocess.run([exe, "--background", "--python", str(SCRIPT), "--", str(blend), str(out), str(jp)],
                          capture_output=True, text=True, timeout=900)


def brightness(png):
    g = Image.open(png).convert("L")
    px = list(g.getdata())
    return px, len(px)


@unittest.skipUnless(blender_exe(), "no Blender executable configured")
class RenderViews(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.dir = Path(cls.tmp.name)
        (d / "in.json").write_text(json.dumps(spec()))
        build_run(blender_exe(), d / "in.json", d / "model", "m")
        cls.blend = d / "model" / "m.blend"
        cls.before = (cls.blend.stat().st_mtime_ns, cls.blend.stat().st_size)
        job = {"stem": "m", "preset": "day", "engine": "cycles", "samples": 8, "width": W, "height": H,
               "north_degrees": 0, "views": ["front", "aerial"], "lines": True,
               "interiors": [{"id": "S01", "name": "Living", "level_z": 0.0, "polygon": [[0, 0], [6, 0], [6, 8], [0, 8]]}],
               "walkthrough": {"kind": "orbit", "frames": 6, "fps": 24, "width": 320, "height": 180, "samples": 4}}
        cls.out = d / "out"
        cls.proc = render(blender_exe(), cls.blend, cls.out, job)
        rp = cls.out / "m_render_report.json"
        cls.rep = json.loads(rp.read_text()) if rp.exists() else {}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_report(self):
        self.assertTrue(self.rep.get("ok"), self.proc.stdout[-2000:] + self.proc.stderr[-2000:])
        self.assertEqual(len(self.rep["stills"]), 3)
        self.assertEqual(self.rep["walkthrough"]["frames"], 6)

    def test_stills_and_lines(self):
        names = ["front", "aerial", "interior_S01"]
        for n in names:
            f = self.out / f"m_day_{n}.png"
            lf = self.out / f"m_day_{n}_lines.png"
            for p in (f, lf):
                im = Image.open(p)
                im.load()
                self.assertEqual(im.size, (W, H), p.name)
            px, total = brightness(lf)
            self.assertGreater(sum(v > 200 for v in px) / total, 0.6, f"{n} lines not mostly white")
            self.assertGreater(sum(v < 100 for v in px) / total, 0.003, f"{n} lines has no dark edges")

    def test_front_exposure(self):
        px, total = brightness(self.out / "m_day_front.png")
        self.assertTrue(40 < sum(px) / total < 215, sum(px) / total)

    def test_frames(self):
        frames = sorted((self.out / "frames" / "m_orbit").glob("frame_*.png"))
        self.assertEqual(len(frames), 6)
        self.assertEqual(Image.open(frames[0]).size, (320, 180))

    def test_blend_untouched(self):
        st = self.blend.stat()
        self.assertEqual((st.st_mtime_ns, st.st_size), self.before)

    def test_missing_blend_fails(self):
        p = render(blender_exe(), self.dir / "nope.blend", self.dir / "o2", {"views": []})
        self.assertNotEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main()
