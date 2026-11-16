"""Plan generator (Phase 3b) and plan checks (Phase 3d).

Approved design brief -> SpecGeometry (rooms, walls, doors, windows) as a reviewable proposal.

Layout strategy: double-loaded corridor (deterministic, explainable, concept-grade):
  * One rectangular footprint for every floor, sized from the largest floor's area and the plot's
    proportions (1.2:1 when the plot is unknown).
  * A 1.2 m corridor runs south-north down the middle; the front door is at its south end.
  * A column of rooms on each side. Every room touches the corridor, so every room has a door
    onto circulation by construction. The stair sits at the north end of the west column, in the
    same place on every floor.
  * Rooms are split between the columns to balance area, then ordered along each column so rooms
    the brief wants connected sit next to each other, public rooms (living, dining, kitchen) toward
    the front on the ground floor. Each column's rooms are scaled to fill it exactly.
  * Walls follow room edges; habitable rooms get windows on their outside walls.
Known limits: rectangles only; one footprint for all floors (a smaller floor gets 'Unallocated'
space); balconies/terraces are not drawn; adjacency across the corridor counts as not achieved.
"""
import math
from collections import defaultdict

from shapely.geometry import LineString, Polygon

from . import brief as brief_mod
from . import geometry as g
from .projects import StudioError

EXT_WALL, INT_WALL, FLOOR_H = 0.23, 0.115, 3.0
CORRIDOR_W, STAIR_MIN_AREA, COL_MIN, COL_MAX = 1.2, 10.0, 3.0, 5.5
PUBLIC = {"living", "dining", "kitchen", "pooja", "garage", "study"}
OUTDOOR = brief_mod.OUTDOOR
CIRCULATION = {"stair", "corridor", "entrance"}
WET = {"bathroom", "wc", "utility"}
NO_WINDOW = {"stair", "corridor", "entrance", "storage", "garage"}
# rule-of-thumb minimum room width (m) for a warning, not code values
MIN_SIDE = {"bedroom": 2.7, "master_bedroom": 3.0, "living": 3.3, "dining": 2.7, "kitchen": 2.1,
            "bathroom": 1.5, "study": 2.1, "garage": 2.8}


# ---------- room placement helpers ----------
def split_columns(rooms: list[dict], west_cap: float, east_cap: float) -> tuple[list, list]:
    """Greedy area balance: biggest rooms first, each to the column with more room left.
    Rooms the brief links stay in the same column when that does not overfill it by > 15%."""
    west, east, side_of = [], [], {}
    left = {"w": west_cap, "e": east_cap}
    for r in sorted(rooms, key=lambda r: -r["area"]):
        linked = {side_of[n] for n in r.get("adjacent_to", []) if n in side_of}
        pick = max(left, key=left.get)
        if len(linked) == 1:
            s = linked.pop()
            if left[s] - r["area"] > -0.15 * (west_cap if s == "w" else east_cap):
                pick = s
        (west if pick == "w" else east).append(r)
        side_of[r["name"]] = pick
        left[pick] -= r["area"]
    return west, east


def order_column(rooms: list[dict], public_first: bool) -> list[dict]:
    """Order rooms front (south) to back so linked rooms are side by side and, on the ground
    floor, public rooms are toward the front. Pairwise-swap hill climb from a size order;
    a column holds a handful of rooms, so this is cheap and fully deterministic."""
    links = {frozenset((r["name"], n)) for r in rooms for n in r.get("adjacent_to", [])}

    def score(order):
        side_by_side = sum(frozenset((a["name"], b["name"])) in links for a, b in zip(order, order[1:]))
        front = sum(len(order) - i for i, r in enumerate(order) if public_first and r["type"] in PUBLIC)
        return side_by_side * 100 + front

    order = sorted(rooms, key=lambda r: (not (public_first and r["type"] in PUBLIC), -r["area"]))
    best, improved = score(order), True
    while improved:
        improved = False
        for i in range(len(order)):
            for j in range(i + 1, len(order)):
                order[i], order[j] = order[j], order[i]
                s = score(order)
                if s > best:
                    best, improved = s, True
                else:
                    order[i], order[j] = order[j], order[i]
    return order


# ---------- generator ----------
def _val(d):
    return d.get("value") if isinstance(d, dict) else d


def generate(brief: dict) -> tuple[g.SpecGeometry, list[str]]:
    req = brief.get("requirements", {})
    schedule = req.get("room_schedule") or []
    if not schedule:
        raise StudioError("the design brief has no room schedule; approve a brief proposal first")
    floors = int(req.get("floors") or 1)
    notes = []
    rooms_by_level = defaultdict(list)
    stair_area, skipped_outdoor = 0.0, []
    for r in schedule:
        area = _val(r.get("area_m2"))
        lvl = min(int(r.get("level", 0)), floors - 1)
        if r["type"] in OUTDOOR:
            skipped_outdoor.append(r["name"])
        elif r["type"] == "stair":
            stair_area = max(stair_area, area or 0)
        elif r["type"] in ("corridor", "entrance"):
            pass                         # replaced by the circulation strip
        elif area:
            rooms_by_level[lvl].append({**r, "area": float(area), "level": lvl})
    if skipped_outdoor:
        notes.append("not drawn yet (outdoor): " + ", ".join(skipped_outdoor))

    stair_a = max(stair_area, STAIR_MIN_AREA) if floors > 1 else 0
    max_area = max((sum(r["area"] for r in rs) for rs in rooms_by_level.values()), default=0)
    if not max_area:
        raise StudioError("the room schedule has no rooms with areas")
    per_floor = max_area + stair_a                     # room area both columns must hold

    pw, pl = _val(brief.get("site", {}).get("plot_width_m")), _val(brief.get("site", {}).get("plot_length_m"))
    ratio = min(max(pw / pl, 0.6), 1.6) if pw and pl else 1.2
    # W = 2*col + corridor, W/D = ratio, 2*col*D = per_floor  ->  solve for the column width
    c = CORRIDOR_W
    depth = (c + math.sqrt(c * c + 4 * ratio * per_floor)) / (2 * ratio)
    col = min(max(per_floor / (2 * depth), COL_MIN), COL_MAX)
    if pw and 2 * col + c > pw:                        # plot too narrow: keep plot width, go deeper
        col = max((pw - c) / 2, COL_MIN)
        notes.append(f"columns narrowed to fit the plot width {pw} m")
    depth = per_floor / (2 * col)
    col, depth = round(col, 2), round(depth, 2)
    width = round(2 * col + c, 2)
    notes.append(f"footprint {width} x {depth} m on every floor: {col} m room columns either side of a "
                 f"{c} m corridor")
    if pw and pl and (width > pw or depth > pl):
        notes.append(f"footprint does NOT fit the {pw} x {pl} m plot")

    levels = [g.Level(id=f"L{i}", name="Ground floor" if i == 0 else f"Floor {i}",
                      elevation_m=i * FLOOR_H, height_m=FLOOR_H, provenance="estimated") for i in range(floors)]
    spaces, sid = [], 0

    def add_space(name, type_, lvl, x0, y0, x1, y1):
        nonlocal sid
        sid += 1
        spaces.append(g.Space(id=f"S{sid:03d}", name=name, type=type_, level=f"L{lvl}",
                              polygon=[(round(x0, 3), round(y0, 3)), (round(x1, 3), round(y0, 3)),
                                       (round(x1, 3), round(y1, 3)), (round(x0, 3), round(y1, 3))],
                              provenance="estimated"))

    stair_h = round(stair_a / col, 2) if floors > 1 else 0
    west_x, east_x = (0.0, col), (col + c, width)
    for lvl in range(floors):
        add_space("Entrance hall" if lvl == 0 else ("Corridor" if floors == 1 else "Landing"),
                  "entrance" if lvl == 0 else "corridor", lvl, col, 0, col + c, depth)
        if floors > 1:
            add_space("Stair", "stair", lvl, *west_x[:1], depth - stair_h, west_x[1], depth)
        west_cap, east_cap = col * (depth - stair_h), col * depth
        west, east = split_columns(rooms_by_level.get(lvl, []), west_cap, east_cap)
        for side, rooms, cap, (x0, x1) in (("west", west, west_cap, west_x), ("east", east, east_cap, east_x)):
            rooms = order_column(rooms, public_first=lvl == 0)
            items = [(r["name"], r["type"], r["area"]) for r in rooms]
            total = sum(a for _, _, a in items)
            # leftover space becomes 'Unallocated' only if it is a usable strip (>= 1.5 m deep);
            # thinner slivers are absorbed by scaling the rooms up slightly
            if not items or (cap - total) / col >= 1.5:
                items.append(("Unallocated", "other", cap - total))
                notes.append(f"L{lvl} {side}: {cap - total:.1f} m2 unallocated")
            elif abs(cap / total - 1) > 0.01:
                notes.append(f"L{lvl} {side}: room areas scaled x{cap / total:.2f} to fill the column")
            scale = cap / sum(a for _, _, a in items)
            y = 0.0
            for name, type_, a in items:
                h = a * scale / col
                add_space(name, type_, lvl, x0, y, x1, y + h)
                y += h

    walls = _walls(spaces, width, depth)
    links = {r["name"]: set(r.get("adjacent_to") or []) for r in schedule}
    openings = _openings(spaces, walls, width, depth, floors, links)
    geo = g.SpecGeometry(levels=levels, spaces=spaces, walls=walls, openings=openings,
                         source={"format": "generated", "units": "m",
                                 "footprint_m": [width, depth], "strategy": "side-hall + squarified treemap"})
    return geo, notes


def _walls(spaces, width, depth) -> list[g.Wall]:
    """Merge every room edge into straight walls; edges on the footprint boundary are exterior."""
    groups = defaultdict(list)           # (level, 'h'|'v', fixed coord) -> [(a, b)]
    for s in spaces:
        p = s.polygon
        for (x0, y0), (x1, y1) in zip(p, p[1:] + p[:1]):
            if abs(y0 - y1) < 1e-6:
                groups[(s.level, "h", round(y0, 3))].append(tuple(sorted((x0, x1))))
            else:
                groups[(s.level, "v", round(x0, 3))].append(tuple(sorted((y0, y1))))
    walls = []
    for (lvl, o, c), spans in sorted(groups.items()):
        spans.sort()
        merged = [list(spans[0])]
        for a, b in spans[1:]:
            if a <= merged[-1][1] + 1e-6:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        for a, b in merged:
            ext = (o == "h" and c in (0, round(depth, 3))) or (o == "v" and c in (0, round(width, 3)))
            start, end = ((a, c), (b, c)) if o == "h" else ((c, a), (c, b))
            walls.append(g.Wall(id=f"W{len(walls) + 1:03d}", level=lvl, start=start, end=end,
                                thickness_m=EXT_WALL if ext else INT_WALL, height_m=FLOOR_H,
                                kind="exterior" if ext else "interior", provenance="estimated"))
    return walls


def _host(point, walls, level):
    pt = LineString([point, point]).centroid
    return min((w for w in walls if w.level == level),
               key=lambda w: LineString([w.start, w.end]).distance(pt)).id


def _shared(a: g.Space, b: g.Space):
    inter = Polygon(a.polygon).boundary.intersection(Polygon(b.polygon).boundary)
    return inter if inter.length > 0 else None


def _openings(spaces, walls, width, depth, floors, links=None) -> list[g.Opening]:
    out, n_d, n_w = [], 0, 0
    by_level = defaultdict(list)
    for s in spaces:
        by_level[s.level].append(s)

    def door(lvl, line, wid=0.9, note=""):
        nonlocal n_d
        n_d += 1
        # 0.15 m in from one end so the leaf swings against a wall, not mid-room
        t = min(0.15 + wid / 2, line.length / 2) / line.length
        pt = line.interpolate(t, normalized=True)
        out.append(g.Opening(id=f"D{n_d:03d}", kind="door", level=lvl, position=(round(pt.x, 3), round(pt.y, 3)),
                             width_m=wid, height_m=2.1, wall=_host((pt.x, pt.y), walls, lvl), provenance="estimated"))

    for lvl, ss in by_level.items():
        hall = next(s for s in ss if s.type in ("entrance", "corridor"))
        if lvl == "L0":                  # front door on the south face of the entrance hall
            (x0, _), (x1, _) = hall.polygon[0], hall.polygon[1]
            door(lvl, LineString([(x0, 0), (x1, 0)]), 1.0)
        for s in ss:
            if s.type in CIRCULATION:
                continue
            line = _shared(s, hall)
            # en-suite bathrooms and wardrobes open from the bedroom they belong to, not the corridor
            if s.type in ("bathroom", "wc", "storage") and links:
                owners = [o for o in ss if o.type in ("bedroom", "master_bedroom")
                          and (o.name in links.get(s.name, ()) or s.name in links.get(o.name, ()))]
                shared = len(owners) != 1 or any(w in s.name.lower() for w in ("shared", "common", "family"))
                own_wall = _shared(s, owners[0]) if owners else None
                if not shared and own_wall is not None and own_wall.length >= 1.0:
                    line = own_wall   # en-suite of exactly one bedroom; shared ones keep the corridor door
            if line is None or line.length < 1.0:
                cands = [(o, _shared(s, o)) for o in ss if o is not s and o.type != "stair"]
                cands = [(o, l) for o, l in cands if l is not None and l.length >= 1.0]
                if not cands:
                    continue             # reported by check_plan as a room with no door
                # a room it is meant to connect to first, else the biggest shared wall
                o, line = max(cands, key=lambda c: (c[0].type in ("living", "dining", "corridor"), c[1].length))
            door(lvl, max(getattr(line, "geoms", [line]), key=lambda l: l.length))
        for s in ss:                     # windows on outside walls
            if s.type in NO_WINDOW:
                continue
            p = s.polygon
            for a, b in zip(p, p[1:] + p[:1]):
                edge = LineString([a, b])
                on_ext = (abs(a[1] - b[1]) < 1e-6 and a[1] in (0, round(depth, 3))) or \
                         (abs(a[0] - b[0]) < 1e-6 and a[0] in (0, round(width, 3)))
                if not on_ext or edge.length < 1.2:
                    continue
                wet = s.type in WET
                wid = 0.6 if wet else round(min(1.8, 0.45 * edge.length), 2)
                mid = edge.interpolate(0.5, normalized=True)
                n_w += 1
                out.append(g.Opening(id=f"N{n_w:03d}", kind="window", level=lvl, position=(round(mid.x, 3), round(mid.y, 3)),
                                     width_m=wid, height_m=0.6 if wet else 1.2, sill_m=1.5 if wet else 0.9,
                                     wall=_host((mid.x, mid.y), walls, lvl), provenance="estimated"))
    return out


# ---------- checks (Phase 3d) ----------
def check_plan(geo: g.SpecGeometry, brief: dict) -> list[str]:
    """Plan-level checks against the brief. Warnings only."""
    warn = []
    sched = {r["name"]: r for r in (brief.get("requirements", {}).get("room_schedule") or [])}
    doors = [o for o in geo.openings if o.kind == "door"]
    by_level = defaultdict(list)
    for s in geo.spaces:
        by_level[s.level].append(s)
    for s in geo.spaces:
        if len(s.polygon) < 3:
            continue
        poly = Polygon(s.polygon)
        xs, ys = [p[0] for p in s.polygon], [p[1] for p in s.polygon]
        short, long_ = sorted((max(xs) - min(xs), max(ys) - min(ys)))
        need = MIN_SIDE.get(s.type)
        if need and short < need:
            warn.append(f"{s.name}: only {short:.2f} m wide (rule of thumb {need} m for a {s.type})")
        if s.type not in CIRCULATION and short and long_ / short > 2.5:
            warn.append(f"{s.name}: long and narrow ({long_:.1f} x {short:.1f} m)")
        target = brief_mod_area(sched.get(s.name))
        if target and abs(poly.area - target) / target > 0.10:
            warn.append(f"{s.name}: {poly.area:.1f} m2 drawn vs {target} m2 in the brief")
        if s.type not in ("stair",) and s.type not in brief_mod.OUTDOOR:
            near = [d for d in doors if d.level == s.level and poly.exterior.distance(LineString([d.position, d.position]).centroid) < 0.05]
            if not near:
                warn.append(f"{s.name}: no door")
            elif s.type in ("bedroom", "master_bedroom", "bathroom", "wc") and not any(
                    _touches_hall(d, by_level[s.level]) for d in near):
                ensuite = s.type in ("bathroom", "wc") and any(
                    _touches(d, by_level[s.level], ("bedroom", "master_bedroom")) for d in near)
                if not ensuite:
                    warn.append(f"{s.name}: reached only through another room, not from the hall")
    names = {s.name: s for s in geo.spaces}
    for pair in brief.get("requirements", {}).get("adjacency") or []:
        a, b = (names.get(n) for n in pair)
        if a and b and a.level == b.level:
            line = _shared(a, b)
            if line is not None and line.length >= 0.9:
                continue
            ya, yb = ([p[1] for p in s.polygon] for s in (a, b))
            hall = next((s for s in by_level[a.level] if s.type in ("entrance", "corridor")), None)
            facing = hall and all(_shared(s, hall) is not None for s in (a, b)) and \
                min(max(ya), max(yb)) - max(min(ya), min(yb)) > 0.5
            warn.append(f"{'across the corridor, not side by side' if facing else 'adjacency not achieved'}: "
                        f"{pair[0]} - {pair[1]}")
    return warn


def propose(slug: str, author: str = "user") -> dict:
    """Generate a layout from the approved brief as a reviewable proposal (imports/I###)."""
    from . import imports, projects
    brief = projects.read_state(slug, "design_brief.yaml")
    geo, notes = generate(brief)
    iid = imports.new_id(slug)
    return imports.save(slug, iid, geo, "generated", notes, author, extra_warnings=check_plan(geo, brief))


def draw(slug: str) -> dict:
    """Draw the APPROVED model geometry: plans/plan_L*.svg|pdf + plans/area_schedule.csv."""
    import csv
    from . import drawing, projects
    spec = projects.read_state(slug, "model_spec.json")
    if not spec.get("walls") and not spec.get("spaces"):
        raise StudioError("model_spec.json has no geometry yet: approve a generated plan or an import first")
    geo = g.SpecGeometry(levels=spec.get("levels", []), spaces=spec.get("spaces", []), walls=spec.get("walls", []),
                         openings=spec.get("openings", []), source=spec.get("geometry_source", {}))
    meta, brief = projects.read_state(slug, "project.json"), projects.read_state(slug, "design_brief.yaml")
    out = projects.project_dir(slug) / "plans"
    src = geo.source
    files = drawing.render(geo, out, {
        "project": meta["name"], "revision": f"r{meta['revision']}",
        "north_degrees": _val(brief.get("site", {}).get("north_degrees")),
        "source": f"{'Generated layout' if src.get('format') == 'generated' else 'Imported from ' + str(src.get('original_name'))}"
                  f" {src.get('import', '')}".strip()})
    sched = out / "area_schedule.csv"
    with sched.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["level", "room", "type", "area_m2", "provenance"])
        for s in geo.spaces:
            w.writerow([s.level, s.name, s.type, s.area_m2 if s.area_m2 is not None else "", s.provenance])
    files.append(sched)
    for f in files:   # drawn from the approved spec, so they are authoritative for this revision
        projects.add_artifact(slug, f"plan_{f.suffix[1:]}", f, "drawing", authoritative=True,
                              note=f"drawn from model revision r{meta['revision']}", source_revision=meta["revision"])
    return {"files": [f.relative_to(projects.project_dir(slug)).as_posix() for f in files],
            "revision": meta["revision"]}


def brief_mod_area(r):
    return _val(r.get("area_m2")) if r else None


def _touches(door: g.Opening, spaces, types) -> bool:
    pt = LineString([door.position, door.position]).centroid
    return any(s.type in types and len(s.polygon) >= 3 and Polygon(s.polygon).exterior.distance(pt) < 0.05
               for s in spaces)


def _touches_hall(door: g.Opening, spaces) -> bool:
    return _touches(door, spaces, ("entrance", "corridor"))
