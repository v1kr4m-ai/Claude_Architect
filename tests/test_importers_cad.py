"""DXF and IFC importer tests. Fixtures are generated in temp dirs; no network, no committed files."""
import json
import os
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.TemporaryDirectory()
os.environ["STUDIO_PROJECTS_ROOT"] = _TMP.name
os.environ["STUDIO_CONFIG"] = str(Path(_TMP.name) / "none.yaml")

import ezdxf  # noqa: E402
import ifcopenshell  # noqa: E402
import ifcopenshell.api.aggregate  # noqa: E402
import ifcopenshell.api.context  # noqa: E402
import ifcopenshell.api.geometry  # noqa: E402
import ifcopenshell.api.root  # noqa: E402
import ifcopenshell.api.spatial  # noqa: E402
import ifcopenshell.api.unit  # noqa: E402
import numpy as np  # noqa: E402

from studio_core import geometry, imports, projects  # noqa: E402
from studio_core.importers import dxf, ifc  # noqa: E402
from studio_core.projects import StudioError  # noqa: E402


def rect(msp, x0, y0, x1, y1, layer):
    for a, b in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))):
        msp.add_line(a, b, dxfattribs={"layer": layer})


def make_dxf(path, units=4, k=1000.0):
    """10 x 8 m house in drawing units (k units per metre), double-line walls 0.2 m apart."""
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = units
    msp = doc.modelspace()
    s = lambda v: v * k
    rect(msp, 0, 0, s(10), s(8), "A-WALL")
    rect(msp, s(.2), s(.2), s(9.8), s(7.8), "A-WALL")
    msp.add_line((s(4.9), s(.2)), (s(4.9), s(7.8)), dxfattribs={"layer": "A-WALL"})   # interior double wall
    msp.add_line((s(5.1), s(.2)), (s(5.1), s(7.8)), dxfattribs={"layer": "A-WALL"})
    for pts, label, at in (([(.2, .2), (4.9, .2), (4.9, 7.8), (.2, 7.8)], "Living Room", (2, 4)),
                           ([(5.1, .2), (9.8, .2), (9.8, 7.8), (5.1, 7.8)], "Bedroom", (7, 4))):
        msp.add_lwpolyline([(s(x), s(y)) for x, y in pts], close=True, dxfattribs={"layer": "A-AREA"})
        msp.add_text(label, dxfattribs={"layer": "A-ANNO", "insert": (s(at[0]), s(at[1]))})
    blk = doc.blocks.new("DOOR_900")
    blk.add_line((0, 0), (s(.9), 0))
    blk.add_arc((0, 0), s(.9), 0, 90)
    msp.add_blockref("DOOR_900", (s(4.9), s(2)))
    doc.saveas(path)


def make_ifc(path):
    f = ifcopenshell.file(schema="IFC4")
    api = ifcopenshell.api
    proj = api.root.create_entity(f, ifc_class="IfcProject", name="P")
    api.unit.assign_unit(f)  # metres
    body = api.context.add_context(f, context_type="Model")
    body = api.context.add_context(f, context_type="Model", context_identifier="Body", target_view="MODEL_VIEW",
                                   parent=body)
    site = api.root.create_entity(f, ifc_class="IfcSite", name="S")
    bld = api.root.create_entity(f, ifc_class="IfcBuilding", name="B")
    st = api.root.create_entity(f, ifc_class="IfcBuildingStorey", name="Ground")
    st.Elevation = 0.0
    api.aggregate.assign_object(f, products=[site], relating_object=proj)
    api.aggregate.assign_object(f, products=[bld], relating_object=site)
    api.aggregate.assign_object(f, products=[st], relating_object=bld)
    for (x, y, ang, length) in ((0, 0, 0, 10), (10, 0, 90, 8), (10, 8, 180, 10), (0, 8, 270, 8)):
        w = api.root.create_entity(f, ifc_class="IfcWall", name="W")
        rep = api.geometry.add_wall_representation(f, context=body, length=length, height=3.0, thickness=0.25)
        api.geometry.assign_representation(f, product=w, representation=rep)
        m = np.eye(4)
        a = np.radians(ang)
        m[:2, :2] = [[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]]
        m[:2, 3] = (x, y)
        api.geometry.edit_object_placement(f, product=w, matrix=m)
        api.spatial.assign_container(f, products=[w], relating_structure=st)
    f.write(str(path))


class DxfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_house(self):
        make_dxf(self.dir / "h.dxf")
        g, notes = dxf.run(self.dir / "h.dxf")
        self.assertEqual(len(g.walls), 5)  # 4 outer + 1 interior, all paired
        for w in g.walls:
            self.assertAlmostEqual(w.thickness_m, 0.2, delta=0.01)
            self.assertEqual(w.provenance, "provided")
        self.assertEqual({s.name: s.type for s in g.spaces}, {"Living Room": "living", "Bedroom": "bedroom"})
        areas = {s.name: s.area_m2 for s in g.spaces}
        self.assertAlmostEqual(areas["Living Room"], 4.7 * 7.6, delta=4.7 * 7.6 * 0.01)
        self.assertAlmostEqual(areas["Bedroom"], 4.7 * 7.6, delta=4.7 * 7.6 * 0.01)
        doors = [o for o in g.openings if o.kind == "door"]
        self.assertEqual(len(doors), 1)
        self.assertAlmostEqual(doors[0].width_m, 0.9, delta=0.02)
        c = geometry.check(g)
        self.assertAlmostEqual(c["summary"]["extent_m"][0], 10, delta=0.3)
        self.assertAlmostEqual(c["summary"]["extent_m"][1], 8, delta=0.3)
        self.assertFalse([w for w in c["warnings"] if "overlap" in w])

    def test_units_guessed(self):
        make_dxf(self.dir / "m.dxf", units=0, k=1.0)
        g, notes = dxf.run(self.dir / "m.dxf")
        self.assertTrue(any("guessed" in n for n in notes))
        self.assertEqual({w.provenance for w in g.walls}, {"estimated"})
        self.assertAlmostEqual(geometry.check(g)["summary"]["extent_m"][0], 10, delta=0.3)

    def test_empty_and_garbage(self):
        ezdxf.new("R2010").saveas(self.dir / "e.dxf")
        with self.assertRaises(StudioError):
            dxf.run(self.dir / "e.dxf")
        (self.dir / "bad.dxf").write_text("not a dxf")
        with self.assertRaises(StudioError):
            dxf.run(self.dir / "bad.dxf")


class IfcTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "box.ifc"
        make_ifc(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_box(self):
        g, notes = ifc.run(self.path)
        self.assertEqual(len(g.levels), 1)
        self.assertEqual(len(g.walls), 4)
        for w in g.walls:
            self.assertAlmostEqual(w.thickness_m, 0.25, delta=0.01)
            self.assertAlmostEqual(w.height_m, 3.0, delta=0.05)
            self.assertEqual(w.provenance, "provided")
        ext = geometry.check(g)["summary"]["extent_m"]
        self.assertAlmostEqual(ext[0], 10, delta=0.3)
        self.assertAlmostEqual(ext[1], 8, delta=0.3)

    def test_empty_ifc(self):
        p = Path(self.tmp.name) / "empty.ifc"
        ifcopenshell.file(schema="IFC4").write(str(p))
        with self.assertRaises(StudioError):
            ifc.run(p)


class EndToEnd(unittest.TestCase):
    def test_import_and_approve(self):
        tmp = Path(tempfile.mkdtemp(dir=_TMP.name))
        make_dxf(tmp / "plan.dxf")
        slug = projects.create_project("CAD import")["slug"]
        rec = imports.import_file(slug, str(tmp / "plan.dxf"))
        self.assertEqual(rec["format"], "dxf")
        imports.approve(slug, rec["id"])
        spec = json.loads((projects.project_dir(slug) / "model_spec.json").read_text(encoding="utf-8"))
        self.assertEqual(len(spec["walls"]), 5)
        self.assertTrue(projects.validate(slug)["valid"])


if __name__ == "__main__":
    unittest.main()
