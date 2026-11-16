"""DXF importer: 2D plan drawings -> SpecGeometry (library: ezdxf).

Supports: walls from LINE / LWPOLYLINE / 2D POLYLINE on wall-like layers (parallel line pairs are merged
into one thickened wall), rooms from closed polylines on room-like layers named by TEXT/MTEXT inside them,
doors and windows from INSERT blocks (by block name).
Limits: single level only (multi-floor drawings are not split), arcs/bulges/splines are ignored, wall
height is unknown, wall kind is not classified, blocks are matched by name only.
"""
import math
import re
from pathlib import Path

import ezdxf
from ezdxf import bbox
from shapely.geometry import Point, Polygon

from .. import brief, geometry
from ..projects import StudioError

UNITS = {1: 0.0254, 2: 0.3048, 4: 0.001, 5: 0.01, 6: 1.0}   # $INSUNITS -> metres
WALL_LAYER = re.compile(r"wall|mur|a-wall", re.I)
ROOM_LAYER = re.compile(r"room|space|area|a-area|zone", re.I)
DOOR = re.compile(r"door|dr\b|dwr", re.I)
WINDOW = re.compile(r"window|win\b|wdw", re.I)


room_type = brief.room_type  # single shared mapping (ifc.py imports it from here)


def _points(e):
    """(points, closed) of a LINE / LWPOLYLINE / 2D POLYLINE, else None."""
    t = e.dxftype()
    if t == "LINE":
        return [tuple(e.dxf.start)[:2], tuple(e.dxf.end)[:2]], False
    if t == "LWPOLYLINE":
        return [tuple(p) for p in e.get_points("xy")], e.closed
    if t == "POLYLINE" and e.is_2d_polyline:
        return [tuple(v.dxf.location)[:2] for v in e.vertices], e.is_closed
    return None


def _segments(pts, closed):
    n = len(pts)
    return [(pts[i], pts[(i + 1) % n]) for i in range(n if closed else n - 1) if n > 1]


def _pair_walls(segs):
    """Merge near-parallel, overlapping segment pairs 0.05-0.60 m apart into (start, end, thickness)."""
    out, used = [], set()
    # ponytail: O(n^2) pair search, fine for house plans; add a spatial index (shapely STRtree) for large plans
    for i, (p, q) in enumerate(segs):
        if i in used:
            continue
        L = math.dist(p, q)
        u = ((q[0] - p[0]) / L, (q[1] - p[1]) / L)
        nrm = (-u[1], u[0])
        best = None
        for j in range(i + 1, len(segs)):
            if j in used:
                continue
            a, b = segs[j]
            Lj = math.dist(a, b)
            if abs(((b[0] - a[0]) * u[1] - (b[1] - a[1]) * u[0]) / Lj) > math.sin(math.radians(2)):
                continue
            d = [((s[0] - p[0]) * nrm[0] + (s[1] - p[1]) * nrm[1]) for s in (a, b)]
            dist = sum(d) / 2
            t = sorted((s[0] - p[0]) * u[0] + (s[1] - p[1]) * u[1] for s in (a, b))
            lo, hi = max(0, t[0]), min(L, t[1])
            if 0.05 <= abs(dist) <= 0.60 and hi - lo > 0.5 * min(L, Lj) and (best is None or abs(dist) < abs(best[0])):
                best = (dist, lo, hi, j)
        if best:
            dist, lo, hi, j = best
            used.add(j)
            off = dist / 2
            out.append(((p[0] + u[0] * lo + nrm[0] * off, p[1] + u[1] * lo + nrm[1] * off),
                        (p[0] + u[0] * hi + nrm[0] * off, p[1] + u[1] * hi + nrm[1] * off), round(abs(dist), 4)))
        else:
            out.append((p, q, None))
    return out


def _block_width(doc, ins):
    try:
        box = bbox.extents(doc.blocks[ins.dxf.name])
        return round(box.size.x * abs(ins.dxf.xscale), 4) if box.has_data and box.size.x > 0 else None
    except Exception:
        return None


def run(path: Path, **opts) -> tuple[geometry.SpecGeometry, list[str]]:
    try:
        doc = ezdxf.readfile(str(path))
    except (IOError, ezdxf.DXFError) as e:
        raise StudioError(f"Not a readable DXF file: {e}") from e
    msp, notes = doc.modelspace(), []

    code = int(doc.header.get("$INSUNITS", 0))
    guessed = code not in UNITS
    if guessed:
        ext = bbox.extents(msp)
        big = ext.has_data and max(ext.size.x, ext.size.y) > 200
        k = 0.001 if big else 1.0
        notes.append(f"Drawing units not set ($INSUNITS={code}); guessed {'millimetres' if big else 'metres'} "
                     "from the drawing size. Check the scale before trusting dimensions.")
    else:
        k = UNITS[code]
    prov = "estimated" if guessed else "provided"

    # --- walls and rooms
    wall_segs, rooms = [], []
    items = [(e, _points(e)) for e in msp if _points(e)]
    any_wall_layer = any(WALL_LAYER.search(e.dxf.layer) for e, _ in items)
    for e, (pts, closed) in items:
        layer = e.dxf.layer
        if closed and ROOM_LAYER.search(layer) and len(pts) >= 3:
            rooms.append(Polygon([(x * k, y * k) for x, y in pts]))
        elif WALL_LAYER.search(layer) if any_wall_layer else not closed:
            wall_segs += [((a[0] * k, a[1] * k), (b[0] * k, b[1] * k)) for a, b in _segments(pts, closed)
                          if math.dist(a, b) * k >= 0.01]
    if not any_wall_layer and wall_segs:
        notes.append("No wall layer found (wall|mur|a-wall); used every open line/polyline segment as a wall.")
    walls = _pair_walls(wall_segs)
    paired = sum(1 for w in walls if w[2] is not None)
    notes.append(f"{len(walls)} walls ({paired} from double lines with thickness, {len(walls) - paired} single "
                 "lines with unknown thickness). Wall heights are unknown.")

    # --- room names from text inside each outline
    texts = []
    for e in msp:
        if e.dxftype() == "TEXT" and e.dxf.text.strip():
            texts.append((e.dxf.text.strip(), e.dxf.insert))
        elif e.dxftype() == "MTEXT" and e.plain_text().strip():
            texts.append((e.plain_text().strip().replace("\n", " "), e.dxf.insert))
    spaces = []
    for n, poly in enumerate(rooms, 1):
        name = next((t for t, p in texts if poly.contains(Point(p[0] * k, p[1] * k))), "")
        if not name:
            notes.append(f"Room outline {n} has no text inside; named 'Room {n}' with type other.")
        spaces.append((name or f"Room {n}", poly))

    # --- doors / windows from blocks
    ops = []
    for e in msp.query("INSERT"):
        kind = "door" if DOOR.search(e.dxf.name) else "window" if WINDOW.search(e.dxf.name) else None
        if kind:
            w = _block_width(doc, e)
            if w is None:
                notes.append(f"{kind} block '{e.dxf.name}': width not computable, left unknown.")
            ops.append((kind, (e.dxf.insert.x * k, e.dxf.insert.y * k), None if w is None else round(w * k, 4)))

    if not walls and not spaces:
        raise StudioError("DXF has no usable walls or room outlines (checked LINE/POLYLINE on wall and room layers).")

    # --- translate to (0, 0) and build
    allp = [p for w in walls for p in w[:2]] + [p for _, s in spaces for p in s.exterior.coords] + [o[1] for o in ops]
    ox, oy = min(p[0] for p in allp), min(p[1] for p in allp)
    mv = lambda p: (round(p[0] - ox, 4), round(p[1] - oy, 4))
    g = geometry.SpecGeometry(
        levels=[geometry.Level(id="L0", name="Level 0", provenance=prov)],
        walls=[geometry.Wall(id=f"W{i:03d}", start=mv(s), end=mv(e), thickness_m=t, provenance=prov)
               for i, (s, e, t) in enumerate(walls, 1)],
        spaces=[geometry.Space(id=f"S{i:03d}", name=n, type=room_type(n), polygon=[mv(p) for p in s.exterior.coords[:-1]],
                               provenance=prov) for i, (n, s) in enumerate(spaces, 1)],
        openings=[geometry.Opening(id=f"{'D' if kd == 'door' else 'N'}{sum(o[0] == kd for o in ops[:i + 1]):03d}",
                                   kind=kd, position=mv(p), width_m=w, provenance=prov)
                  for i, (kd, p, w) in enumerate(ops)],
        source={"units": f"m (scale {k})", "units_guessed": guessed})
    notes.append("Single level assumed; multi-floor drawings are not split into levels.")
    return g, notes
