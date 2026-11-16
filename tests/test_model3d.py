"""model3d: materials, roof, file naming and the plan-vs-model consistency check (no Blender needed)."""
import copy
import tempfile
import unittest
from pathlib import Path

from studio_core import model3d

SPEC = {
    "levels": [{"id": "L0", "elevation_m": 0.0, "height_m": 3.0}, {"id": "L1", "elevation_m": 3.0, "height_m": 3.0}],
    "walls": [{"id": f"W{i}", "level": "L0", "start": s, "end": e, "thickness_m": 0.23}
              for i, (s, e) in enumerate([((0, 0), (10, 0)), ((10, 0), (10, 8)), ((10, 8), (0, 8)), ((0, 8), (0, 0))])],
    "openings": [{"id": "D1", "kind": "door"}, {"id": "N1", "kind": "window"}, {"id": "N2", "kind": "window"}],
    "spaces": [{"id": "S1", "polygon": [[0, 0], [10, 0], [10, 8]], "type": "living"},
               {"id": "S2", "polygon": [], "type": "bedroom"},
               {"id": "S3", "polygon": [[0, 6], [3, 6], [3, 8]], "type": "stair"}],
}
ROOF = {"type": "flat", "thickness_m": 0.25, "overhang_m": 0.3, "parapet_m": 0.0}


def good_report():
    lo, hi = [-0.415, -0.415, -0.2], [10.415, 8.415, 6.25]
    return {"objects": {"walls_built": ["W0", "W1", "W2", "W3"], "windows": 2, "doors": 1, "floors": 2,
                        "stair_steps": 17},
            "bbox_min": lo, "bbox_max": hi,
            "by_level": {"L0": {"z_min": -0.2, "z_max": 3.0}, "L1": {"z_min": 2.8, "z_max": 6.0}},
            "glb": {"bytes": 50000, "reimport": {"meshes": 40, "bbox_min": lo, "bbox_max": hi,
                                                 "spec_ids": ["W0", "W1", "W2", "W3", "D1"]}}}


class Consistency(unittest.TestCase):
    def failed(self, report):
        return {c["check"] for c in model3d.consistency(SPEC, report, ROOF) if not c["ok"]}

    def test_matching_model_passes(self):
        self.assertEqual(self.failed(good_report()), set())

    def test_each_defect_is_caught(self):
        cases = {
            "every wall built": lambda r: r["objects"]["walls_built"].remove("W2"),
            "windows count": lambda r: r["objects"].update(windows=1),
            "doors count": lambda r: r["objects"].update(doors=0),
            "room floors": lambda r: r["objects"].update(floors=3),
            "model footprint matches plan": lambda r: r["bbox_max"].__setitem__(0, 12.0),
            "building height": lambda r: r["bbox_max"].__setitem__(2, 4.0),
            "L1 height": lambda r: r["by_level"]["L1"].update(z_max=5.5),
            "stairs built": lambda r: r["objects"].update(stair_steps=0),
            "GLB re-import matches": lambda r: r["glb"]["reimport"].update(meshes=0),
            "GLB keeps element ids": lambda r: r["glb"]["reimport"]["spec_ids"].remove("W1"),
        }
        for name, break_it in cases.items():
            r = copy.deepcopy(good_report())
            break_it(r)
            failed = self.failed(r)
            self.assertIn(name, failed, name)
            if name != "model footprint matches plan":   # a wider bbox also breaks the GLB bbox match
                noise = {"GLB re-import matches"} - {name}   # bbox edits can also break the GLB bbox match
                self.assertEqual(failed - noise, {name}, name)


class Inputs(unittest.TestCase):
    def test_materials_from_brief(self):
        mats, notes = model3d.materials({"materials": ["White render", "Black aluminium frames", "Unobtainium"]})
        self.assertEqual(mats["exterior_wall"], list(model3d.PALETTE[0][1]))
        self.assertEqual(mats["frame"], [0.06, 0.06, 0.07])
        self.assertTrue(any("Unobtainium" in n for n in notes))
        self.assertEqual(model3d.materials({})[0]["roof"], list(model3d.DEFAULTS["roof"]))
        mats, notes = model3d.materials({"materials": ["Concrete", "Glass", "Metal"]})
        self.assertEqual((mats["exterior_wall"], mats["frame"]), ([0.52, 0.52, 0.5], [0.25, 0.26, 0.28]))
        self.assertFalse(any("not recognised" in n for n in notes))

    def test_roof_from_brief(self):
        self.assertEqual(model3d.roof_settings({"roof": "Flat roof with minimal overhangs"})["overhang_m"], 0.15)
        self.assertEqual(model3d.roof_settings({"roof": "Gable roof, clay tiles"})["type"], "gable")
        self.assertEqual(model3d.roof_settings({"roof": "flat with parapet"})["parapet_m"], 0.9)

    def test_never_overwrites_an_existing_blend(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "blender").mkdir()
            self.assertEqual(model3d._stem(p, 9), "model_r0009")
            (p / "blender" / "model_r0009.blend").write_bytes(b"x")
            self.assertEqual(model3d._stem(p, 9), "model_r0009_b2")


if __name__ == "__main__":
    unittest.main()
