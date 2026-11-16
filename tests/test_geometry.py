"""Geometry schema and checks."""
import unittest

from pydantic import ValidationError

from studio_core import geometry as g


def box_walls(w=10, l=8, t=0.2):
    pts = [(0, 0), (w, 0), (w, l), (0, l)]
    return [g.Wall(id=f"W{i:03d}", start=pts[i], end=pts[(i + 1) % 4], thickness_m=t, provenance="provided")
            for i in range(4)]


class Schema(unittest.TestCase):
    def test_area_computed_and_bad_shapes_rejected(self):
        s = g.Space(id="S001", polygon=[(0, 0), (4, 0), (4, 3), (0, 3)])
        self.assertEqual(s.area_m2, 12.0)
        with self.assertRaises(ValidationError):
            g.Space(id="S002", polygon=[(0, 0), (4, 3), (4, 0), (0, 3)])   # bow-tie
        with self.assertRaises(ValidationError):
            g.Wall(id="W1", start=(1, 1), end=(1, 1))
        with self.assertRaises(ValidationError):
            g.SpecGeometry(levels=[g.Level(id="L0")], walls=[g.Wall(id="W1", level="L9", start=(0, 0), end=(1, 0))])

    def test_check_summary_and_warnings(self):
        spaces = [g.Space(id="S001", name="Living", polygon=[(0, 0), (6, 0), (6, 8), (0, 8)]),
                  g.Space(id="S002", name="Bed", polygon=[(5, 0), (10, 0), (10, 8), (5, 8)]),   # overlaps 8 m2
                  g.Space(id="S003", name="Shaft", polygon=[(0, 0), (0.5, 0), (0.5, 0.5)])]
        walls = box_walls() + [g.Wall(id="W005", start=(6, 0), end=(6, 8))]
        r = g.check(g.SpecGeometry(levels=[g.Level(id="L0")], spaces=spaces, walls=walls,
                                   openings=[g.Opening(id="D001", kind="door", position=(6, 4), width_m=9.0)]))
        self.assertEqual(r["summary"]["extent_m"], [10, 8])
        self.assertEqual((r["summary"]["walls"], r["summary"]["doors"]), (5, 1))
        w = "\n".join(r["warnings"])
        for expected in ("S001 and S002 overlap", "under 1 m2", "1 of 5 walls have unknown thickness",
                         "width 9.0 m is unusual"):
            self.assertIn(expected, w)

    def test_units_sanity(self):
        mm = g.check(g.SpecGeometry(walls=box_walls(10000, 8000)))
        self.assertIn("implausible", "\n".join(mm["warnings"]))
        empty = g.check(g.SpecGeometry())
        self.assertIn("no walls and no rooms", "\n".join(empty["warnings"]))


if __name__ == "__main__":
    unittest.main()
