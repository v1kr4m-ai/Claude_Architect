"""Floor-plan drawing renderer."""
import tempfile
import unittest
from pathlib import Path

from studio_core import geometry as g
from studio_core.drawing import render
from studio_core.projects import StudioError


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def building(w=10.0, h=8.0):
    pts = [(0, 0), (w, 0), (w, h), (0, h)]
    walls = [g.Wall(id=f"W{i}", level="L0", start=pts[i], end=pts[(i + 1) % 4], thickness_m=0.23, kind="exterior", provenance="provided")
             for i in range(4)]
    return walls


def sample():
    walls = building() + [
        g.Wall(id="WI", level="L0", start=(6, 0), end=(6, 8), thickness_m=0.115, kind="interior", provenance="provided"),
        g.Wall(id="WX", level="L0", start=(0, 4), end=(3, 4), thickness_m=None)]
    return g.SpecGeometry(
        levels=[g.Level(id="L0", name="Ground"), g.Level(id="L1", name="First")],
        walls=walls,
        spaces=[g.Space(id="S1", name="Living", type="living", polygon=rect(0, 0, 6, 8), provenance="provided"),
                g.Space(id="S2", name="Bedroom", type="bedroom", polygon=rect(6, 0, 10, 8), provenance="provided"),
                g.Space(id="S3", name="Store", type="other", area_m2=4.0)],
        openings=[g.Opening(id="D1", kind="door", level="L0", position=(6, 4), wall="WI", width_m=0.9, provenance="provided"),
                  g.Opening(id="N1", kind="window", level="L0", position=(3, 0), wall="W0", width_m=1.2, provenance="provided")])


class Drawing(unittest.TestCase):
    def test_sheet_contents(self):
        with tempfile.TemporaryDirectory() as d:
            files = render(sample(), Path(d) / "out", {"project": "Demo"})
            self.assertEqual(sorted(p.name for p in files), ["plan_L0.pdf", "plan_L0.svg"])
            self.assertEqual(sorted(p.name for p in (Path(d) / "out").iterdir()), ["plan_L0.pdf", "plan_L0.svg"])
            self.assertTrue((Path(d) / "out" / "plan_L0.pdf").read_bytes().startswith(b"%PDF"))
            svg = (Path(d) / "out" / "plan_L0.svg").read_text(encoding="utf-8")
            for s in ("Living", "Bedroom", "Store", "1:100", "CONCEPT", "48.0", "32.0", "verify on site", "not located on plan"):
                self.assertIn(s, svg)

    def test_rerender_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            render(sample(), Path(d), {})
            self.assertEqual(len(render(sample(), Path(d), {"north_degrees": 30})), 2)

    def test_scale_choice(self):
        with tempfile.TemporaryDirectory() as d:
            big = g.SpecGeometry(walls=building(60, 20))
            render(big, Path(d), {})
            t = (Path(d) / "plan_L0.svg").read_text(encoding="utf-8")
            self.assertTrue("1:500" in t or "1:200" in t)
            small = g.SpecGeometry(walls=building(4, 4))
            render(small, Path(d), {})
            self.assertIn("1:50 at A3", (Path(d) / "plan_L0.svg").read_text(encoding="utf-8"))

    def test_empty_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(StudioError):
                render(g.SpecGeometry(levels=[g.Level(id="L0")]), Path(d), {})


if __name__ == "__main__":
    unittest.main()
