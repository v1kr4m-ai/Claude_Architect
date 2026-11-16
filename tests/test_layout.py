"""Phase 3 layout generator and plan checks."""
import os
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.TemporaryDirectory()
os.environ["STUDIO_PROJECTS_ROOT"] = _TMP.name
os.environ["STUDIO_CONFIG"] = str(Path(_TMP.name) / "none.yaml")

from shapely.geometry import Polygon  # noqa: E402
from shapely.ops import unary_union  # noqa: E402

from studio_core import geometry, imports, layout, projects  # noqa: E402
from studio_core.projects import StudioError, dim  # noqa: E402


def room(name, type_, level, area, adj=()):
    return {"name": name, "type": type_, "level": level, "area_m2": dim(area, "estimated"), "adjacent_to": list(adj)}


BRIEF = {
    "requirements": {"floors": 2, "room_schedule": [
        room("Living", "living", 0, 24, ["Kitchen"]), room("Kitchen", "kitchen", 0, 11, ["Living"]),
        room("Guest bed", "bedroom", 0, 11), room("WC", "wc", 0, 3), room("Stair", "stair", 0, 10),
        room("Master", "master_bedroom", 1, 16, ["Ensuite"]), room("Ensuite", "bathroom", 1, 5, ["Master"]),
        room("Bed 2", "bedroom", 1, 12), room("Bed 3", "bedroom", 1, 11), room("Bath", "bathroom", 1, 5),
        room("Balcony", "balcony", 1, 6)],
        "adjacency": [["Living", "Kitchen"], ["Master", "Ensuite"]]},
    "site": {"plot_width_m": dim(12, "provided"), "plot_length_m": dim(20, "provided")},
}


class Generate(unittest.TestCase):
    def setUp(self):
        self.geo, self.notes = layout.generate(BRIEF)

    def test_rooms_tile_the_footprint_without_overlap(self):
        w, d = self.geo.source["footprint_m"]
        for lvl in ("L0", "L1"):
            polys = [Polygon(s.polygon) for s in self.geo.spaces if s.level == lvl]
            self.assertAlmostEqual(unary_union(polys).area, w * d, delta=0.05)          # no gaps
            self.assertAlmostEqual(sum(p.area for p in polys), w * d, delta=0.05)       # no overlaps
        self.assertEqual(geometry.check(self.geo)["warnings"], [])

    def test_every_room_has_access_and_stair_is_stacked(self):
        warns = layout.check_plan(self.geo, BRIEF)
        self.assertFalse([x for x in warns if "no door" in x or "only through" in x], warns)
        stairs = [s.polygon for s in self.geo.spaces if s.type == "stair"]
        self.assertEqual(len(stairs), 2)
        self.assertEqual(stairs[0], stairs[1])                                          # same place each floor
        self.assertEqual(sum(o.kind == "door" and o.width_m == 1.0 for o in self.geo.openings), 1)  # front door

    def test_linked_rooms_placed_side_by_side_and_ensuite_opens_from_bedroom(self):
        warns = "\n".join(layout.check_plan(self.geo, BRIEF))
        self.assertNotIn("adjacency not achieved: Master - Ensuite", warns)
        ens = next(s for s in self.geo.spaces if s.name == "Ensuite")
        master = next(s for s in self.geo.spaces if s.name == "Master")
        doors = [o for o in self.geo.openings if o.kind == "door" and o.level == "L1" and
                 Polygon(ens.polygon).exterior.distance(Polygon([o.position] * 3).centroid) < 0.05]
        self.assertTrue(any(Polygon(master.polygon).exterior.distance(Polygon([d.position] * 3).centroid) < 0.05
                            for d in doors))

    def test_shared_bathroom_opens_from_corridor(self):
        # live bug: a bathroom linked to two bedrooms was treated as an en-suite of one of them
        b = {**BRIEF, "requirements": {**BRIEF["requirements"], "room_schedule": BRIEF["requirements"]["room_schedule"] + [
            room("Shared Bathroom", "bathroom", 1, 5, ["Bed 2", "Bed 3"])]}}
        geo, _ = layout.generate(b)
        sb = next(s for s in geo.spaces if s.name == "Shared Bathroom")
        hall = next(s for s in geo.spaces if s.level == "L1" and s.type == "corridor")
        door = next(o for o in geo.openings if o.kind == "door" and o.level == "L1" and
                    Polygon(sb.polygon).exterior.distance(Polygon([o.position] * 3).centroid) < 0.05)
        self.assertLess(Polygon(hall.polygon).exterior.distance(Polygon([door.position] * 3).centroid), 0.05)

    def test_windows_only_on_outside_walls_and_notes(self):
        w, d = self.geo.source["footprint_m"]
        for o in self.geo.openings:
            if o.kind == "window":
                x, y = o.position
                self.assertTrue(min(abs(x), abs(x - w), abs(y), abs(y - d)) < 0.01, o)
        self.assertTrue(any("Balcony" in n for n in self.notes))

    def test_errors(self):
        with self.assertRaises(StudioError):
            layout.generate({"requirements": {"room_schedule": []}})


class Flow(unittest.TestCase):
    def test_propose_approve_draw(self):
        slug = projects.create_project("Layout Flow House")["slug"]
        projects.write_state(slug, "design_brief.yaml", {**projects.read_state(slug, "design_brief.yaml"), **BRIEF})
        rec = layout.propose(slug)
        self.assertEqual((rec["format"], rec["status"]), ("generated", "proposed"))
        d = projects.project_dir(slug)
        self.assertTrue(all((d / f).exists() for f in rec["previews"]) and len(rec["previews"]) == 4)
        with self.assertRaises(StudioError):
            layout.draw(slug)                        # nothing approved yet
        imports.approve(slug, rec["id"])
        out = layout.draw(slug)
        self.assertIn("plans/plan_L0.pdf", out["files"])
        self.assertIn("plans/area_schedule.csv", out["files"])
        csv_text = (d / "plans" / "area_schedule.csv").read_text(encoding="utf-8")
        self.assertIn("Ensuite", csv_text)
        arts = projects.read_state(slug, "artifacts.json")["artifacts"]
        self.assertTrue(any(a["path"] == "plans/plan_L1.svg" and a["authoritative"] for a in arts))
        revs = {a["revision"] for a in arts if a["kind"] in ("plan_svg", "plan_pdf", "plan_csv")}
        self.assertEqual(revs, {out["revision"]})                 # every sheet records the spec revision it shows
        self.assertTrue(projects.validate(slug)["valid"])
        # change the model: the drawings are now stale until drawn again
        rec2 = layout.propose(slug)
        imports.approve(slug, rec2["id"])
        v = projects.validate(slug)
        self.assertFalse(v["valid"])
        self.assertTrue(any("run draw again" in x for x in v["problems"]))
        layout.draw(slug)
        self.assertTrue(projects.validate(slug)["valid"])


if __name__ == "__main__":
    unittest.main()
