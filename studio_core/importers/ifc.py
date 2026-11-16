"""IFC importer: BIM models -> SpecGeometry (library: ifcopenshell).

Supports: storeys as levels, walls (IfcWall*) as centre lines with thickness from the footprint's minimum
rotated rectangle, IfcSpace footprints as rooms, IfcDoor / IfcWindow as openings with width/height/sill.
Geometry comes from triangulated shapes in world coordinates (metres).
Limits: curved or L-shaped walls are reduced to their bounding rectangle, space footprints use the outer ring
(holes dropped), wall kind (exterior/interior) is not classified, elements that fail to triangulate are skipped.
"""
import math
from pathlib import Path

import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.element
import ifcopenshell.util.unit
from shapely.geometry import Polygon
from shapely.ops import unary_union

from .. import geometry
from ..projects import StudioError
from .dxf import room_type


def _shape(settings, el):
    """(footprint polygon, z_min, z_max) of an element in world metres."""
    g = ifcopenshell.geom.create_shape(settings, el).geometry
    v, f = g.verts, g.faces
    pts = [(v[i], v[i + 1], v[i + 2]) for i in range(0, len(v), 3)]
    tris = [Polygon([pts[f[i + k]][:2] for k in range(3)]) for i in range(0, len(f), 3)]
    fp = unary_union([t for t in tris if t.area > 1e-9]).simplify(0.005)
    if fp.is_empty:
        raise ValueError("empty footprint")
    zs = [p[2] for p in pts]
    return fp, min(zs), max(zs)


def _outline(fp):
    """Largest polygon of a footprint (shapely may return a multi-part union)."""
    return max(fp.geoms, key=lambda g: g.area) if hasattr(fp, "geoms") else fp


def _wall_line(fp):
    """Centre line and thickness from the minimum rotated rectangle."""
    c = list(fp.minimum_rotated_rectangle.exterior.coords)[:4]
    length = lambda i: math.dist(c[i], c[(i + 1) % 4])
    short = sorted(range(4), key=length)[:2]
    mid = [((c[i][0] + c[(i + 1) % 4][0]) / 2, (c[i][1] + c[(i + 1) % 4][1]) / 2) for i in short]
    return mid[0], mid[1], length(short[0])


def run(path: Path, **opts) -> tuple[geometry.SpecGeometry, list[str]]:
    try:
        model = ifcopenshell.open(str(path))
    except Exception as e:
        raise StudioError(f"Not a readable IFC file: {e}") from e
    notes, k = [], ifcopenshell.util.unit.calculate_unit_scale(model)
    settings = ifcopenshell.geom.settings()
    settings.set("use-world-coords", True)

    # --- levels
    storeys = sorted(model.by_type("IfcBuildingStorey"), key=lambda s: s.Elevation or 0)
    elev = [(s.Elevation or 0) * k for s in storeys]
    if storeys:
        levels = [geometry.Level(id=f"L{i}", name=s.Name or s.LongName or f"Level {i}", elevation_m=round(elev[i], 4),
                                 height_m=round(elev[i + 1] - elev[i], 4) if i + 1 < len(storeys) else None,
                                 provenance="provided") for i, s in enumerate(storeys)]
        notes.append(f"{len(levels)} storeys; the top storey height is unknown (no storey above it).")
    else:
        levels, elev = [geometry.Level(id="L0", name="Level 0", provenance="unknown")], [0.0]
        notes.append("No IfcBuildingStorey found; everything placed on L0 at elevation 0 (unknown).")

    def level_of(el, zmin):
        c = ifcopenshell.util.element.get_container(el)
        if c is not None and c in storeys:
            return storeys.index(c)
        return min(range(len(elev)), key=lambda i: abs(elev[i] - zmin)) if storeys else 0

    def shapes(kind):
        for el in model.by_type(kind):
            try:
                yield el, *_shape(settings, el)
            except Exception as e:
                notes.append(f"Skipped {kind} {el.GlobalId}: could not build geometry ({e}).")

    walls, spaces, ops = [], [], []
    for el, fp, z0, z1 in shapes("IfcWall"):
        s, e, th = _wall_line(fp)
        walls.append((s, e, th, z1 - z0, level_of(el, z0)))
    for el, fp, z0, z1 in shapes("IfcSpace"):
        poly = _outline(fp)
        if not poly.is_valid:
            poly = poly.convex_hull
            notes.append(f"Space {el.GlobalId} outline was invalid; replaced by its convex hull.")
        name = (el.LongName or el.Name or "").strip()
        spaces.append((name or f"Space {len(spaces) + 1}", poly, level_of(el, z0)))
    for kind, label in (("IfcDoor", "door"), ("IfcWindow", "window")):
        for el, fp, z0, z1 in shapes(kind):
            li = level_of(el, z0)
            w, h = (getattr(el, a, None) for a in ("OverallWidth", "OverallHeight"))
            if w is None:
                notes.append(f"{label} {el.GlobalId}: OverallWidth missing, width left unknown.")
            ops.append((label, (fp.centroid.x, fp.centroid.y), w and w * k, h and h * k,
                        round(z0 - elev[li], 4) if label == "window" else None, li))

    if not walls and not spaces:
        raise StudioError("IFC has no walls or spaces with usable geometry.")
    notes.append(f"{len(walls)} walls, {len(spaces)} spaces, {len(ops)} doors/windows (all values from the file).")

    allp = [p for w in walls for p in w[:2]] + [p for _, s, _ in spaces for p in s.exterior.coords] + [o[1] for o in ops]
    ox, oy = min(p[0] for p in allp), min(p[1] for p in allp)
    mv = lambda p: (round(p[0] - ox, 4), round(p[1] - oy, 4))
    r = lambda x: None if x is None else round(x, 4)
    cnt = {"door": 0, "window": 0}
    openings = []
    for kd, p, w, h, sill, li in ops:
        cnt[kd] += 1
        openings.append(geometry.Opening(id=f"{'D' if kd == 'door' else 'N'}{cnt[kd]:03d}", kind=kd, level=f"L{li}",
                                         position=mv(p), width_m=r(w), height_m=r(h), sill_m=sill, provenance="provided"))
    g = geometry.SpecGeometry(
        levels=levels,
        walls=[geometry.Wall(id=f"W{i:03d}", level=f"L{li}", start=mv(s), end=mv(e), thickness_m=r(th), height_m=r(h),
                             provenance="provided") for i, (s, e, th, h, li) in enumerate(walls, 1)],
        spaces=[geometry.Space(id=f"S{i:03d}", name=n, type=room_type(n), level=f"L{li}",
                               polygon=[mv(p) for p in poly.exterior.coords[:-1]], provenance="provided")
                for i, (n, poly, li) in enumerate(spaces, 1)],
        openings=openings, source={"units": "m", "unit_scale": k, "schema": model.schema})
    return g, notes
