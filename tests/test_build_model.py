"""Blender headless model build: runs the real Blender once (skipped when none is available)."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "engines" / "blender" / "build_model.py"


def blender_exe():
    exe = os.environ.get("BLENDER_EXE")
    if not exe:
        exe = (yaml.safe_load((ROOT / "config" / "studio.yaml").read_text(encoding="utf-8")).get("blender") or {}).get("executable")
    return exe if exe and Path(exe).is_file() else None


def run(exe, *args):
    return subprocess.run([exe, "--background", "--factory-startup", "--python", str(SCRIPT), "--", *map(str, args)],
                          capture_output=True, text=True, timeout=600)


def spec():
    levels, walls, spaces, openings = [], [], [], []
    for i, lid in enumerate(("L0", "L1")):
        levels.append({"id": lid, "name": ("Ground", "First")[i] + " floor", "elevation_m": 3.0 * i, "height_m": 3.0})
        for n, (a, b, k, t) in enumerate((((0, 0), (10, 0), "exterior", 0.23), ((10, 0), (10, 8), "exterior", 0.23),
                                          ((10, 8), (0, 8), "exterior", 0.23), ((0, 8), (0, 0), "exterior", 0.23),
                                          ((6, 0), (6, 8), "interior", 0.115)), 1):
            walls.append({"id": f"W{i}0{n}", "level": lid, "start": a, "end": b, "thickness_m": t, "height_m": None, "kind": k})
        spaces += [{"id": f"S{i}1", "name": "Living", "type": "living", "level": lid, "polygon": [[0, 0], [6, 0], [6, 8], [0, 8]], "area_m2": 48},
                   {"id": f"S{i}2", "name": "Bed", "type": "bedroom", "level": lid, "polygon": [[6, 0], [10, 0], [10, 8], [6, 8]], "area_m2": 32},
                   {"id": f"S{i}3", "name": "Stair", "type": "stair", "level": lid, "polygon": [[0, 6], [3, 6], [3, 8], [0, 8]], "area_m2": 6}]
        openings += [{"id": f"D{i}01", "kind": "door", "level": lid, "position": [3, 0], "width_m": 1.0, "height_m": 2.1, "sill_m": None, "wall": f"W{i}01"},
                     {"id": f"D{i}02", "kind": "door", "level": lid, "position": [6, 4], "width_m": 0.9, "height_m": 2.1, "sill_m": None, "wall": f"W{i}05"},
                     {"id": f"N{i}01", "kind": "window", "level": lid, "position": [8, 0], "width_m": 1.65, "height_m": 1.2, "sill_m": 0.9, "wall": f"W{i}01"},
                     {"id": f"N{i}02", "kind": "window", "level": lid, "position": [10, 4], "width_m": 1.65, "height_m": 1.2, "sill_m": 0.9, "wall": f"W{i}02"}]
    return {"project": "T", "revision": 1, "levels": levels, "spaces": spaces, "walls": walls, "openings": openings,
            "settings": {"slab_thickness_m": 0.2, "default_wall_thickness_m": 0.2, "default_level_height_m": 3.0, "door_height_m": 2.1,
                         "window_sill_m": 0.9, "window_height_m": 1.2,
                         "roof": {"type": "flat", "thickness_m": 0.25, "overhang_m": 0.3, "parapet_m": 0.0}},
            "materials": {k: [0.6, 0.6, 0.6] for k in ("exterior_wall", "interior_wall", "slab", "roof", "glass", "frame", "door",
                                                       "floor_dry", "floor_wet", "stair")}}


@unittest.skipUnless(blender_exe(), "no Blender executable configured")
class BuildModel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.spec = spec()
        (cls.dir / "in.json").write_text(json.dumps(cls.spec))
        cls.proc = run(blender_exe(), cls.dir / "in.json", cls.dir / "out", "m")
        rp = cls.dir / "out" / "m_report.json"
        cls.rep = json.loads(rp.read_text()) if rp.exists() else {}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_outputs(self):
        self.assertTrue(self.rep.get("ok"), self.proc.stdout[-2000:] + self.proc.stderr[-2000:])
        out = self.dir / "out"
        self.assertTrue((out / "m.blend").exists() and (out / "m_preview.png").exists())
        self.assertGreater((out / "m.glb").stat().st_size, 10_000)

    def test_counts(self):
        o = self.rep["objects"]
        self.assertTrue({w["id"] for w in self.spec["walls"]} <= set(o["walls_built"]))
        self.assertEqual((o["windows"], o["doors"], o["slabs"], o["floors"], o["roof"]), (4, 4, 2, 6, 1))
        self.assertGreater(o["stair_steps"], 0)

    def test_bbox(self):
        lo, hi = self.rep["bbox_min"], self.rep["bbox_max"]
        self.assertTrue(10.2 <= hi[0] - lo[0] <= 10.9, (lo, hi))
        self.assertTrue(8.2 <= hi[1] - lo[1] <= 8.9, (lo, hi))
        self.assertGreaterEqual(hi[2], 6.0)
        self.assertEqual(set(self.rep["by_level"]), {"L0", "L1"})
        self.assertIn("W001", self.rep["by_level"]["L0"]["walls"])

    def test_glb_roundtrip(self):
        r = self.rep["glb"]["reimport"]
        self.assertGreater(r["meshes"], 0)
        for a, b in zip(r["bbox_min"] + r["bbox_max"], self.rep["bbox_min"] + self.rep["bbox_max"]):
            self.assertAlmostEqual(a, b, delta=0.05)
        self.assertTrue({"W001", "D001", "N001"} <= set(r["spec_ids"]), r["spec_ids"])

    def test_missing_input_fails(self):
        p = run(blender_exe(), self.dir / "nope.json", self.dir / "o2", "x")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("input file not found", p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
