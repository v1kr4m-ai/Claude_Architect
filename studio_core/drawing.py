"""Floor-plan sheets (A3 landscape, vector SVG + PDF) from a SpecGeometry, one sheet per level.

Drawn: room fills (wet / circulation tints) with name, area and side lengths; walls as filled
bands (dashed centreline when thickness is unknown); door leaf + swing arc; window triple lines;
openings cut out of the wall fill; overall and wall-chain dimensions (bottom, left); scale bar;
north arrow; area schedule (rooms without a polygon are listed as "not located on plan");
legend; title block; a warning note when any element is estimated or unknown.

Known limits: straight walls only; no stair/fixture symbols beyond the room label; dimension
chain covers only the bottom exterior wall; openings without a host wall within 0.5 m are skipped.
Everything is drawn in sheet millimetres (true scale), with the object API only (thread-safe).
"""
import datetime
import math
import re
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
from matplotlib import rc_context  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.patches import Arc, PathPatch, Rectangle
from matplotlib.patches import Polygon as MPoly  # noqa: E402
from matplotlib.path import Path as MPath  # noqa: E402
from shapely.geometry import LineString, Point, Polygon  # noqa: E402
from shapely.geometry.polygon import orient  # noqa: E402
from shapely.ops import unary_union  # noqa: E402

from .geometry import Level, SpecGeometry  # noqa: E402
from .projects import StudioError  # noqa: E402

SHEET_W, SHEET_H = 420.0, 297.0
AREA = (10.0, 28.0, 270.0, 259.0)          # drawing area x, y, w, h (mm)
FIT_W, FIT_H = 270.0, 250.0
PX, PW = 295.0, 115.0                      # right panel x, width
PT = 0.3528                                # mm per point
WET, CIRC = {"bathroom", "wc", "utility"}, {"corridor", "stair", "stairs", "entrance"}
FILL = {"room": "#f3efe6", "wet": "#dcebf0", "circ": "#ebe4d0"}
PROV_COL = {"provided": "#1b7a3a", "measured": "#1f5fa8", "estimated": "#c26a00", "unknown": "#c0261b"}
WALL, DIM = "#262626", "#444444"
NOTE = "Contains estimated / unknown dimensions — verify on site."


def _paths(geom):
    """shapely (Multi)Polygon -> one compound matplotlib path (holes wound opposite)."""
    polys = [geom] if geom.geom_type == "Polygon" else [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon"]
    verts, codes = [], []
    for p in polys:
        p = orient(p, 1.0)
        for ring in [p.exterior, *p.interiors]:
            pts = list(ring.coords)
            verts += pts
            codes += [MPath.MOVETO] + [MPath.LINETO] * (len(pts) - 2) + [MPath.CLOSEPOLY]
    return MPath(verts, codes) if verts else None


def _rect(c, d, hl, ht):
    n = (-d[1], d[0])
    return Polygon([(c[0] + d[0] * a + n[0] * b, c[1] + d[1] * a + n[1] * b) for a, b in ((-hl, -ht), (hl, -ht), (hl, ht), (-hl, ht))])


def _host(o, walls):
    w = walls.get(o.wall)
    if w is None:
        near = sorted((LineString([v.start, v.end]).distance(Point(o.position)), v.id) for v in walls.values())
        w = walls[near[0][1]] if near and near[0][0] <= 0.5 else None
    if w is None:
        return None
    (x0, y0), (x1, y1) = w.start, w.end
    ln = math.hypot(x1 - x0, y1 - y0)
    d = ((x1 - x0) / ln, (y1 - y0) / ln)
    s = (o.position[0] - x0) * d[0] + (o.position[1] - y0) * d[1]
    return w, d, (x0 + d[0] * s, y0 + d[1] * s)


_LEVEL_PREFIX = re.compile(r"^\s*(ground|first|second|third|top|upper|lower)\s+floor\s*[-:–]?\s*|^\s*(level|floor)\s*\d+\s*[-:–]?\s*", re.I)


def short_name(name: str) -> str:
    """'Ground Floor - Open Living and Dining' -> 'Open Living and Dining' (the sheet names the floor)."""
    return _LEVEL_PREFIX.sub("", name).strip() or name


def _fmt_room(s, poly):
    """Label lines [(text, bold)]: name (wrapped to two lines when long), area, side lengths."""
    name = short_name(s.name or s.id)
    words = name.split()
    if len(name) > 14 and len(words) > 1:   # split at the word boundary nearest the middle
        cut = min(range(1, len(words)), key=lambda i: abs(len(" ".join(words[:i])) - len(name) / 2))
        lines = [(" ".join(words[:cut]), True), (" ".join(words[cut:]), True)]
    else:
        lines = [(name, True)]
    lines.append((f"{s.area_m2:.1f} m²", False))
    minx, miny, maxx, maxy = poly.bounds
    if abs(poly.area - (maxx - minx) * (maxy - miny)) < 1e-6:
        lines.append((f"{maxx - minx:.2f} × {maxy - miny:.2f}", False))
    return lines


def _draw_level(geo, level, idx, n, title):
    walls = {w.id: w for w in geo.walls if w.level == level.id}
    spaces = [s for s in geo.spaces if s.level == level.id]
    opens = [o for o in geo.openings if o.level == level.id]
    thick = [w for w in walls.values() if w.thickness_m]
    thin = [w for w in walls.values() if not w.thickness_m]

    wall_geo = unary_union([LineString([w.start, w.end]).buffer(w.thickness_m / 2, cap_style="flat") for w in thick])
    cuts, doors, wins = [], [], []
    for o in opens:
        h = _host(o, walls)
        if h is None:
            continue
        w, d, c = h
        t = w.thickness_m or 0.1
        default = 0.9 if o.kind == "door" else 1.2
        wd = o.width_m or default
        if w.thickness_m:
            cuts.append(_rect(c, d, wd / 2, (t + 0.02) / 2))
        (doors if o.kind == "door" else wins).append((d, c, wd, t, o.width_m is None))
    if cuts and not wall_geo.is_empty:
        wall_geo = wall_geo.difference(unary_union(cuts))

    polys = [(s, Polygon(s.polygon)) for s in spaces if len(s.polygon) >= 3]
    boxes = [g.bounds for g in [wall_geo, *[LineString([w.start, w.end]) for w in thin], *[p for _, p in polys]] if not g.is_empty]
    if boxes:
        minx, miny = min(b[0] for b in boxes), min(b[1] for b in boxes)
        maxx, maxy = max(b[2] for b in boxes), max(b[3] for b in boxes)
    else:
        minx = miny = 0.0
        maxx = maxy = 5.0
    ew, eh = maxx - minx, maxy - miny
    scale = next((sc for sc in (50, 100, 200, 500) if (ew + 4) * 1000 / sc <= FIT_W and (eh + 4) * 1000 / sc <= FIT_H), 500)
    k = 1000.0 / scale
    X0 = AREA[0] + (AREA[2] - ew * k) / 2
    Y0 = AREA[1] + (AREA[3] - eh * k) / 2

    def T(p):
        return (X0 + (p[0] - minx) * k, Y0 + (p[1] - miny) * k)

    fig = Figure(figsize=(SHEET_W / 25.4, SHEET_H / 25.4), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, SHEET_W)
    ax.set_ylim(0, SHEET_H)
    ax.set_aspect("equal")
    ax.axis("off")

    def text(x, y, s, size=6.5, bold=False, ha="left", va="center", color="#111", **kw):
        ax.text(x, y, s, fontsize=size, fontweight="bold" if bold else "normal", ha=ha, va=va, color=color, zorder=6, **kw)

    def line(a, b, lw=0.4, color=DIM, z=4, **kw):
        ax.plot([a[0], b[0]], [a[1], b[1]], lw=lw, color=color, zorder=z, solid_capstyle="butt", **kw)

    ax.add_patch(Rectangle((5, 5), SHEET_W - 10, SHEET_H - 10, fill=False, lw=0.8, ec="#111"))
    line((290, 5), (290, SHEET_H - 5), 0.5, "#111")

    # --- rooms
    for s, p in polys:
        kind = "wet" if s.type in WET else "circ" if s.type in CIRC else "room"
        ax.add_patch(MPoly([T(q) for q in p.exterior.coords], closed=True, fc=FILL[kind], ec="#777", lw=0.3, zorder=1))
    tags = []
    for i, s in enumerate(spaces, 1):
        if len(s.polygon) < 3:
            continue
        p = Polygon(s.polygon)
        rp = T(p.representative_point().coords[0])
        bw, bh = (p.bounds[2] - p.bounds[0]) * k * 0.9, (p.bounds[3] - p.bounds[1]) * k * 0.9
        full = _fmt_room(s, p)
        placed = False
        for lines in (full, full[:-1] if len(full) > 2 else full):  # drop side lengths before giving up
            for size in (8, 7, 6, 5.5, 5, 4.5):
                tw = max(len(t) * (0.62 if b else 0.55) for t, b in lines) * size * PT
                if tw <= bw and len(lines) * 1.35 * size * PT <= bh:
                    y = rp[1] + (len(lines) - 1) * 0.675 * size * PT
                    for t, b in lines:
                        text(rp[0], y, t, size, b, "center")
                        y -= 1.35 * size * PT
                    placed = True
                    break
            if placed:
                break
        if not placed:
            text(rp[0], rp[1], str(i), 6, True, "center")
            tags.append(i)

    # --- walls
    path = _paths(wall_geo) if not wall_geo.is_empty else None
    if path:
        path = MPath([T(v) for v in path.vertices], path.codes)
        ax.add_patch(PathPatch(path, fc=WALL, ec=WALL, lw=0.2, zorder=2))
    for w in thin:
        line(T(w.start), T(w.end), 0.5, "#8a8a8a", 2, ls=(0, (4, 2)))

    # --- openings
    for d, c, wd, t, guess in doors:
        j = (c[0] - d[0] * wd / 2, c[1] - d[1] * wd / 2)
        tip = (j[0] - d[1] * wd, j[1] + d[0] * wd)
        line(T(j), T(tip), 0.6, "#111", 3)
        a0 = math.degrees(math.atan2(d[1], d[0]))
        cj = T(j)
        ax.add_patch(Arc(cj, 2 * wd * k, 2 * wd * k, theta1=a0, theta2=a0 + 90, lw=0.4, color="#111", zorder=3))
        if guess:
            text(*T((c[0] - d[1] * wd * 0.6, c[1] + d[0] * wd * 0.6)), "?", 7, True, "center", color="#c0261b")
    for d, c, wd, t, guess in wins:
        nv = (-d[1], d[0])  # wall normal ('n' is the sheet count: reusing it broke the title block)
        for off in (-t / 2, 0, t / 2):
            line(T((c[0] - d[0] * wd / 2 + nv[0] * off, c[1] - d[1] * wd / 2 + nv[1] * off)),
                 T((c[0] + d[0] * wd / 2 + nv[0] * off, c[1] + d[1] * wd / 2 + nv[1] * off)), 0.35, "#111", 3)
        if guess:
            text(*T((c[0] + nv[0] * (t / 2 + 0.3), c[1] + nv[1] * (t / 2 + 0.3))), "?", 7, True, "center", color="#c0261b")

    # --- dimensions (sheet mm, outside the building)
    def dim_h(x0, x1, y, label):
        for x in (x0, x1):
            line((x, Y0 - 2), (x, y - 1.5), 0.25)
            line((x - 0.8, y - 0.8), (x + 0.8, y + 0.8), 0.5)
        line((x0, y), (x1, y), 0.3)
        if x1 - x0 >= 5:
            text((x0 + x1) / 2, y + 1.2, label, 5.5, ha="center", va="bottom")

    def dim_v(y0, y1, x, label):
        for y in (y0, y1):
            line((X0 - 2, y), (x - 1.5, y), 0.25)
            line((x - 0.8, y - 0.8), (x + 0.8, y + 0.8), 0.5)
        line((x, y0), (x, y1), 0.3)
        text(x - 1.2, (y0 + y1) / 2, label, 5.5, ha="right", rotation=90)

    xl, xr = X0, X0 + ew * k
    dim_h(xl, xr, Y0 - 13, f"{ew:.2f}")
    dim_v(Y0, Y0 + eh * k, X0 - 13, f"{eh:.2f}")
    horiz = [w for w in thick if abs(w.end[1] - w.start[1]) < 1e-3]
    if horiz:
        bw = min(horiz, key=lambda w: w.start[1])
        yb, tb = bw.start[1], bw.thickness_m
        xs = sorted({round(v[0], 3) for w in thick if w is not bw for v in (w.start, w.end)
                     if abs(v[1] - yb) <= tb / 2 + 0.05 and minx + 0.3 < v[0] < maxx - 0.3})
        pts = [minx, *xs, maxx]
        if len(pts) > 2:
            for a, b in zip(pts, pts[1:]):
                dim_h(T((a, 0))[0], T((b, 0))[0], Y0 - 6, f"{b - a:.2f}")

    # --- scale bar, north arrow
    bar = 5 if scale <= 100 else 10
    for m in range(bar):
        ax.add_patch(Rectangle((AREA[0] + 5 + m * k, 14), k, 1.6, fc="#111" if m % 2 == 0 else "white", ec="#111", lw=0.4, zorder=4))
    for m in range(0, bar + 1, 1 if bar == 5 else 5):
        text(AREA[0] + 5 + m * k, 18.5, f"{m}" + (" m" if m == bar else ""), 6, ha="center")
    if title.get("north_degrees") is not None:
        a = math.radians(title["north_degrees"])
        cx, cy = AREA[0] + AREA[2] - 12, AREA[1] + AREA[3] - 14
        ax.annotate("", xy=(cx + 9 * math.sin(a), cy + 9 * math.cos(a)), xytext=(cx - 9 * math.sin(a), cy - 9 * math.cos(a)),
                    arrowprops=dict(arrowstyle="-|>", color="#111", lw=1.0), zorder=5)
        text(cx + 12.5 * math.sin(a), cy + 12.5 * math.cos(a), "N", 9, True, "center")
    if not boxes:
        text(AREA[0] + AREA[2] / 2, AREA[1] + AREA[3] / 2, "No geometry located on plan", 10, ha="center", color="#888")

    # --- schedule
    rows = len(spaces) + sum(len(s.polygon) < 3 for s in spaces) + 1
    rh = min(4.4, (272 - 135) / rows)
    fs = min(6.5, rh / PT * 0.6)
    text(PX + 2, 286, "AREA SCHEDULE", 9, True)
    cols = (PX + 2, PX + 8, PX + 47, PX + 89, PX + 92)
    for x, h, ha in zip(cols, ("#", "Room", "Type", "m²", "Source"), ("left", "left", "left", "right", "left")):
        text(x, 279, h, 6.5, True, ha)
    line((PX, 276.5), (PX + PW, 276.5), 0.4, "#111")
    y = 273
    for i, s in enumerate(spaces, 1):
        located = len(s.polygon) >= 3
        text(cols[0], y, str(i), fs)
        text(cols[1], y, short_name(s.name or s.id)[:26], fs)
        text(cols[2], y, s.type[:18], fs, color="#555")
        text(cols[3], y, f"{s.area_m2:.1f}" if s.area_m2 is not None else "—", fs, ha="right")
        text(cols[4], y, s.provenance, fs, color=PROV_COL[s.provenance])
        y -= rh
        if not located:
            text(cols[1] + 2, y, "not located on plan", fs * 0.9, color="#888", style="italic")
            y -= rh
    total = sum(s.area_m2 or 0 for s in spaces)
    line((PX, y + rh * 0.5), (PX + PW, y + rh * 0.5), 0.4, "#111")
    text(cols[1], y, "Total room area", fs, True)
    text(cols[3], y, f"{total:.1f}", fs, True, "right")
    if tags:
        text(PX + 2, y - rh, "Rooms " + ", ".join(map(str, tags)) + " are numbered on the plan (too small to label).", 5, color="#555")

    # --- legend
    ly = 124
    text(PX + 2, ly, "LEGEND", 7.5, True)
    items = [("wall", "Wall"), ("door", "Door (leaf and swing)"), ("win", "Window"),
             ("room", "Room"), ("wet", "Wet room (bath, wc, utility)"), ("circ", "Circulation (corridor, stair, entrance)")]
    if thin:
        items.append(("thin", "Wall thickness unknown"))
    for key, label in items:
        ly -= 5.2
        x, yy = PX + 3, ly
        if key == "wall":
            ax.add_patch(Rectangle((x, yy - 1), 9, 2, fc=WALL, ec=WALL, lw=0.2))
        elif key == "door":
            line((x, yy - 2), (x, yy + 2.5), 0.6, "#111")
            ax.add_patch(Arc((x, yy - 2), 9, 9, theta1=0, theta2=90, lw=0.4, color="#111"))
        elif key == "win":
            for o in (-0.8, 0, 0.8):
                line((x, yy + o), (x + 9, yy + o), 0.35, "#111")
        elif key == "thin":
            line((x, yy), (x + 9, yy), 0.5, "#8a8a8a", ls=(0, (4, 2)))
        else:
            ax.add_patch(Rectangle((x, yy - 1.5), 9, 3, fc=FILL[key], ec="#777", lw=0.3))
        text(PX + 15, yy, label, 6)
    flagged = any(e.provenance in ("estimated", "unknown") for e in [*spaces, *walls.values(), *opens])
    if flagged:
        text(PX + 2, 84, "Contains estimated / unknown", 6.5, True, color="#c0261b")
        text(PX + 2, 80, "dimensions — verify on site.", 6.5, True, color="#c0261b")

    # --- title block
    ax.add_patch(Rectangle((PX, 10), PW, 62, fill=False, lw=0.8, ec="#111"))
    text(PX + 3, 66, title.get("project") or "Untitled project", 10, True)
    for i, (s, b) in enumerate([(f"{level.name or level.id} ({level.id})", True), (f"Scale 1:{scale} at A3", False),
                                (f"Revision: {title.get('revision') or '-'}", False),
                                (f"Date: {datetime.date.today().isoformat()}", False),
                                (title.get("status") or "CONCEPT — NOT FOR CONSTRUCTION", True),
                                (title.get("source") or "", False), (f"Sheet {idx} of {n}", False)]):
        text(PX + 3, 59 - i * 6.8, s, 7.5, b)
    return fig, scale


def render(geo: SpecGeometry, out_dir: Path, title: dict, formats=("svg", "pdf")) -> list[Path]:
    """One A3-landscape sheet per level that has walls or spaces. Returns written files."""
    title = title or {}
    ids = [l.id for l in geo.levels] + [e.level for e in [*geo.walls, *geo.spaces] if e.level not in {l.id for l in geo.levels}]
    levels = {l.id: l for l in geo.levels}
    drawable = [levels.get(i) or Level(id=i) for i in dict.fromkeys(ids)
                if any(e.level == i for e in [*geo.walls, *geo.spaces])]
    if not drawable:
        raise StudioError("nothing to draw")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for n, level in enumerate(drawable, 1):
        fig, _ = _draw_level(geo, level, n, len(drawable), title)
        for ext in formats:
            p = out_dir / f"plan_{level.id}.{ext}"
            with rc_context({"svg.fonttype": "none", "pdf.fonttype": 42}):
                fig.savefig(p, format=ext)
            written.append(p)
    return written
