"""Phase 2 tests: brief proposals, deterministic planning checks, approval flow. No model needed."""
import json
import os
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.TemporaryDirectory()
os.environ["STUDIO_PROJECTS_ROOT"] = _TMP.name
os.environ["STUDIO_CONFIG"] = str(Path(_TMP.name) / "none.yaml")

from studio_core import brief, projects  # noqa: E402
from studio_core.projects import StudioError  # noqa: E402

REQ = ("Two-storey family house on a 12 x 20 m plot. 3 bedrooms, master bedroom 16 sqm, "
       "open living and kitchen, 2 bathrooms, a pooja room. Target about 180 m2 built up.")


def room(name, type_, level, area, src="suggested", adj=()):
    return {"name": name, "type": type_, "level": level, "area_m2": area, "area_source": src,
            "adjacent_to": list(adj), "notes": ""}


def proposal(**over):
    p = {"summary": "Compact two-storey home", "building_type": "residence", "floors": 2, "style": "modern",
         "plot_width_m": {"value": 12, "source": "user"}, "plot_length_m": {"value": 20, "source": "user"},
         "target_gross_area_m2": {"value": 130, "source": "suggested"},
         "rooms": [room("Living", "living", 0, 22, adj=["Kitchen", "Stair G", "Pooja"]),
                   room("Kitchen", "kitchen", 0, 11, adj=["Living"]),
                   room("Pooja", "pooja", 0, 3, adj=["Living"]),
                   room("WC", "wc", 0, 2, adj=["Living"]),
                   room("Stair G", "stair", 0, 6, adj=["Living", "Stair 1"]),
                   room("Stair 1", "stair", 1, 6, adj=["Landing"]),
                   room("Landing", "corridor", 1, 6, adj=["Master", "Bed 2", "Bed 3", "Bath"]),
                   room("Master", "master_bedroom", 1, 16, "user", adj=["Landing"]),
                   room("Bed 2", "bedroom", 1, 11, adj=["Landing"]),
                   room("Bed 3", "bedroom", 1, 10, adj=["Landing"]),
                   room("Bath", "bathroom", 1, 4, adj=["Landing"]),
                   room("Balcony", "balcony", 1, 5)],
         "concepts": [{"name": "Courtyard", "massing": "L", "roof": "flat", "facade_materials": ["render"],
                       "tradeoffs": "light vs area"},
                      {"name": "Box", "massing": "box", "roof": "flat", "facade_materials": ["brick"],
                       "tradeoffs": "cheap"}],
         "reference_influences": [], "assumptions": ["no basement"], "open_questions": ["parking?"]}
    p.update(over)
    return brief.ProposalLLM.model_validate(p)


class Check(unittest.TestCase):
    def test_provenance_verified_against_text(self):
        r = brief.check(proposal(), REQ, set())
        master = next(x for x in r["rooms"] if x["name"] == "Master")
        bed2 = next(x for x in r["rooms"] if x["name"] == "Bed 2")
        self.assertEqual(master["area_m2"]["provenance"], "provided")   # 16 is in the text
        self.assertEqual(bed2["area_m2"]["provenance"], "estimated")
        self.assertEqual(r["site"]["plot_width_m"]["provenance"], "provided")
        lied = proposal(rooms=[room("Living", "living", 0, 37, "user", ["Kitchen"]),
                               room("Kitchen", "kitchen", 0, 9, adj=["Living"])], floors=1)
        liv = brief.check(lied, REQ, set())["rooms"][0]
        self.assertEqual(liv["area_m2"]["provenance"], "estimated")      # 37 is not in the text
        self.assertIn("no matching area", liv["area_m2"]["note"])

    def test_clean_proposal_has_no_warnings_and_correct_areas(self):
        r = brief.check(proposal(), REQ, set())
        self.assertEqual(r["warnings"], [])
        self.assertEqual(r["areas"]["net_m2_by_level"], {"0": 44.0, "1": 53.0})   # balcony excluded
        self.assertEqual(r["areas"]["net_m2"], 97.0)
        self.assertEqual(r["areas"]["gross_m2_estimate"], round(97 * brief.GROSS_FACTOR, 1))
        self.assertIn(["Living", "Kitchen"] if "Kitchen" > "Living" else ["Kitchen", "Living"], r["adjacency"])

    def test_planning_warnings(self):
        bad = proposal(rooms=[room("Living", "living", 0, 9, adj=["Ghost"]),
                              room("Bed", "bedroom", 3, 12),
                              room("Bed", "bedroom", 1, 12)],
                       plot_width_m={"value": 5, "source": "suggested"},
                       plot_length_m={"value": 2, "source": "suggested"},
                       reference_influences=[{"reference": "REF999", "takes": "roof"}])
        w = "\n".join(brief.check(bad, REQ, {"REF001"})["warnings"])
        for expected in ("duplicate room name: Bed", "unknown room 'Ghost'", "level 3 but the building has 2",
                         "below the 12 m2 rule of thumb", "no adjacency", "level 0: no stair",
                         "level 1: no stair", "larger than the plot", "unknown reference REF999",
                         "differs from target"):
            self.assertIn(expected, w)

    def test_unknown_room_type_coerced(self):
        p = proposal(rooms=[room("Gym", "Home Gym", 0, 15, adj=["Car"]), room("Car", "Parking", 0, 5, adj=["Gym"])],
                     floors=1)
        self.assertEqual([r.type for r in p.rooms], ["other", "garage"])

    def test_plot_number_does_not_verify_room_area(self):
        # live bug: "12" from "12 x 20 m plot" verified a 12 m2 bedroom as provided
        p = proposal(rooms=[room("Guest", "bedroom", 0, 12, "user", ["Hall"]),
                            room("Hall", "corridor", 0, 20, "user", ["Guest"])], floors=1)
        r = brief.check(p, REQ, set())
        self.assertEqual([x["area_m2"]["provenance"] for x in r["rooms"]], ["estimated", "estimated"])
        self.assertEqual(r["site"]["plot_length_m"]["provenance"], "provided")
        self.assertEqual(brief._numbers_in("master 16 sqm, 3.5 m2 wc, 1,200 sq m")["area"], [16, 3.5, 1200])


class Fake:
    name = "fake"

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def chat(self, model, prompt, **kw):
        self.calls.append(kw)
        return self.replies.pop(0)


class Flow(unittest.TestCase):
    n = 0

    def setUp(self):
        Flow.n += 1
        self.slug = projects.create_project(f"Brief House {Flow.n}")["slug"]
        self.good = json.dumps(proposal().model_dump())

    def test_propose_writes_proposal_not_brief(self):
        before = (projects.project_dir(self.slug) / "design_brief.yaml").read_bytes()
        fake = Fake(["not json", self.good])
        rec = brief.propose(self.slug, REQ, provider=fake, model="m")
        self.assertEqual((rec["id"], rec["status"], rec["validation_retries"]), ("P001", "proposed", 1))
        self.assertEqual(fake.calls[0]["max_tokens"], 4000)
        d = projects.project_dir(self.slug)
        self.assertEqual(before, (d / "design_brief.yaml").read_bytes())
        self.assertTrue((d / "proposals" / "P001.md").exists())
        self.assertIn("Questions for you", (d / "proposals" / "P001.md").read_text(encoding="utf-8"))
        self.assertTrue(projects.validate(self.slug)["valid"])

    def test_approve_applies_concept_with_backup(self):
        brief.propose(self.slug, REQ, provider=Fake([self.good]), model="m")
        out = brief.approve(self.slug, "P001", concept=1)
        b = projects.read_state(self.slug, "design_brief.yaml")
        self.assertEqual(b["preferences"]["concept"], "Box")
        self.assertEqual(b["preferences"]["materials"], ["brick"])
        self.assertEqual(len(b["requirements"]["room_schedule"]), 12)
        self.assertEqual(b["site"]["plot_width_m"], {"value": 12.0, "provenance": "provided",
                                                     "note": "stated in requirements"})
        self.assertEqual(b["approved_decisions"], [out["decision"]])
        dec = projects.read_state(self.slug, "decisions.json")["decisions"][0]
        self.assertEqual(dec["status"], "approved")
        d = projects.project_dir(self.slug)
        self.assertTrue(any(f.name.endswith("design_brief.yaml") for f in (d / "backups").iterdir()))
        self.assertEqual(brief.load(self.slug, "P001")["status"], "approved")
        with self.assertRaises(StudioError):
            brief.approve(self.slug, "P001")          # cannot approve twice
        self.assertTrue(projects.validate(self.slug)["valid"])

    def test_reject_and_bad_ids(self):
        brief.propose(self.slug, REQ, provider=Fake([self.good]), model="m")
        brief.reject(self.slug, "P001", "too big")
        with self.assertRaises(StudioError):
            brief.approve(self.slug, "P001")
        for bad in ("../x", "P1", "P001.json", ""):
            with self.assertRaises(StudioError):
                brief.load(self.slug, bad)
        with self.assertRaises(StudioError):
            brief.propose(self.slug, "house", provider=Fake([]), model="m")

    def test_decision_status(self):
        d = projects.add_decision(self.slug, "Flat roof", "", "agent")
        self.assertEqual(projects.set_decision_status(self.slug, d["id"], "approved")["status"], "approved")
        with self.assertRaises(StudioError):
            projects.set_decision_status(self.slug, "D999", "approved")


if __name__ == "__main__":
    unittest.main()
