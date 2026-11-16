"""Build a 3D model from the studio's spec JSON, headless.

  blender --background --factory-startup --python build_model.py -- <input.json> <out_dir> <stem>

Writes <stem>.blend, <stem>.glb, <stem>_preview.png and <stem>_report.json into <out_dir>.
Metres, x east, y north, z up. Every object is named, collected per level and carries the custom
properties `spec_id` (source element id) and `level` (level id); they survive the GLB (extras).

Built: walls as separate box pieces (no booleans) with openings cut by construction; window glass +
frame; door leaves; one floor slab per level; floor finishes per space; dog-leg stairs; flat roof;
ground plane; materials; sun, world, camera; .blend, render, GLB and a GLB re-import check.

Known limits: stairs are indicative; the slab is the overlapping prisms of the level's space
polygons in one object (not a boolean union; coplanar overlaps are hidden under floor finishes);
the roof is always flat, over the bounding rectangle of the top level's walls grown by the overhang;
corner joins are plain overlaps; roof / site carry no `level` property.
"""
import json
import math
import re
import sys
import time
import traceback
from pathlib import Path

import bpy
from mathutils import Vector

EPS = 0.001          # pieces shorter than this (m) are skipped
WET = {"bathroom", "wc", "utility"}
notes = []


# ---------------------------------------------------------------- geometry accumulators
def add_box(g, o, u, t0, t1, s0, s1, z0, z1):
    """Box at origin o, spanning t0..t1 along unit vector u and s0..s1 along its left normal."""
    n = (-u[1], u[0])
    base = len(g[0])
    for t, s in ((t0, s0), (t1, s0), (t1, s1), (t0, s1)):
        for z in (z0, z1):
            g[0].append((o[0] + u[0] * t + n[0] * s, o[1] + u[1] * t + n[1] * s, z))
    # verts: 0=(t0,s0,z0) 1=(.,.,z1) 2=(t1,s0,z0) 3 4=(t1,s1,z0) 5 6=(t0,s1,z0) 7
    g[1].extend(tuple(base + i for i in f) for f in
             ((0, 6, 4, 2), (1, 3, 5, 7), (0, 2, 3, 1), (2, 4, 5, 3), (4, 6, 7, 5), (6, 0, 1, 7)))


def add_prism(g, poly, z0, z1):
    pts = [tuple(p) for p in poly]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(pts, pts[1:] + pts[:1]))
    if area < 0:
        pts.reverse()
    n, base = len(pts), len(g[0])
    g[0].extend([(x, y, z0) for x, y in pts] + [(x, y, z1) for x, y in pts])
    g[1].extend([tuple(base + i for i in range(n - 1, -1, -1)), tuple(base + n + i for i in range(n))])
    g[1].extend((base + i, base + (i + 1) % n, base + n + (i + 1) % n, base + n + i) for i in range(n))


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


# ---------------------------------------------------------------- scene helpers
class Ctx:
    pass


def material(name, rgb, roughness=0.6, glass=False):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    bsdf = next(n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    bsdf.inputs["Base Color"].default_value = (*rgb[:3], 1)
    bsdf.inputs["Roughness"].default_value = 0.05 if glass else roughness
    if glass:
        key = "Transmission Weight" if "Transmission Weight" in bsdf.inputs else "Transmission"
        bsdf.inputs[key].default_value = 1.0
    return m


def collection(name, parent=None):
    c = bpy.data.collections.new(name)
    (parent or bpy.context.scene.collection).children.link(c)
    return c


def make(name, g, coll, mat, **props):
    if not g[1]:
        return None
    me = bpy.data.meshes.new(name)
    me.from_pydata(g[0], [], g[1])
    me.update()
    ob = bpy.data.objects.new(name, me)
    coll.objects.link(ob)
    me.materials.append(mat)
    for k, v in props.items():
        ob[k] = v
    return ob


def fail(msg):
    raise SystemExit(f"build_model error: {msg}")


# ---------------------------------------------------------------- elements
def match_openings(spec, walls):
    """wall id -> openings (explicit `wall`, else nearest centreline within 0.3 m on the same level)."""
    by_wall = {w["id"]: [] for w in walls}
    for o in spec["openings"]:
        wid = o.get("wall")
        if wid is None:
            best = None
            for w in walls:
                if w["level"] != o["level"]:
                    continue
                (sx, sy), (ex, ey) = w["start"], w["end"]
                dx, dy = ex - sx, ey - sy
                t = max(0, min(1, ((o["position"][0] - sx) * dx + (o["position"][1] - sy) * dy) / (dx * dx + dy * dy or 1)))
                d = math.hypot(o["position"][0] - sx - t * dx, o["position"][1] - sy - t * dy)
                if d < 0.3 and (best is None or d < best[0]):
                    best = (d, w["id"])
            wid = best and best[1]
        if wid not in by_wall:
            notes.append(f"opening {o['id']} has no matching wall; skipped")
            continue
        by_wall[wid].append(o)
    return by_wall


def build_walls(c, spec, lv):
    st = spec["settings"]
    out = {"pieces": 0, "built": [], "windows": 0, "doors": 0}
    by_wall = match_openings(spec, spec["walls"])
    for w in spec["walls"]:
        lvl = lv[w["level"]]
        z0 = lvl["elevation_m"]
        H = w.get("height_m") or lvl["h"]
        T = w.get("thickness_m") or st["default_wall_thickness_m"]
        (sx, sy), (ex, ey) = w["start"], w["end"]
        L = math.hypot(ex - sx, ey - sy)
        if L < EPS:
            notes.append(f"wall {w['id']} has zero length; skipped")
            continue
        u = ((ex - sx) / L, (ey - sy) / L)
        spans = []
        for o in by_wall[w["id"]]:
            t = (o["position"][0] - sx) * u[0] + (o["position"][1] - sy) * u[1]
            a0, a1 = max(0.0, t - o["width_m"] / 2), min(L, t + o["width_m"] / 2)
            if a0 != t - o["width_m"] / 2 or a1 != t + o["width_m"] / 2:
                notes.append(f"opening {o['id']} runs past the end of wall {w['id']}; clamped")
            if a1 - a0 < EPS:
                notes.append(f"opening {o['id']} lies outside wall {w['id']}; skipped")
                continue
            win = o["kind"] == "window"
            sill = o.get("sill_m")
            sill = (st["window_sill_m"] if win else 0.0) if sill is None else sill
            h = o.get("height_m") or (st["window_height_m"] if win else st["door_height_m"])
            if sill + h > H:
                h = H - sill
                notes.append(f"opening {o['id']} taller than wall {w['id']}; height reduced")
            spans.append((a0, a1, sill, h, o, win))
        spans.sort(key=lambda s: s[0] + s[1])
        mat = c.mat["exterior_wall" if w.get("kind") == "exterior" else "interior_wall"]
        pieces, cur = [], 0.0
        for a0, a1, sill, h, o, win in spans:
            pieces += [(cur, a0, 0.0, H), (a0, a1, 0.0, sill), (a0, a1, sill + h, H)]
            cur = max(cur, a1)
        pieces.append((cur, L, 0.0, H))
        n = 0
        for t0, t1, za, zb in pieces:
            if t1 - t0 < EPS or zb - za < EPS:
                continue
            n += 1
            g = ([], [])
            add_box(g, (sx, sy), u, t0, t1, -T / 2, T / 2, z0 + za, z0 + zb)
            make(f"WALL_{w['id']}_{n}", g, c.col[w["level"]]["Walls"], mat, spec_id=w["id"], level=w["level"])
        out["pieces"] += n
        out["built"].append(w["id"])
        for a0, a1, sill, h, o, win in spans:
            col, p = c.col[w["level"]]["Openings"], dict(spec_id=o["id"], level=w["level"])
            if win:
                glass, frame, f = ([], []), ([], []), 0.05
                add_box(glass, (sx, sy), u, a0, a1, -0.01, 0.01, z0 + sill, z0 + sill + h)
                for b in ((a0, a0 + f, sill, sill + h), (a1 - f, a1, sill, sill + h),
                          (a0 + f, a1 - f, sill, sill + f), (a0 + f, a1 - f, sill + h - f, sill + h)):
                    add_box(frame, (sx, sy), u, b[0], b[1], -0.04, 0.04, z0 + b[2], z0 + b[3])
                make(f"WIN_{o['id']}_glass", glass, col, c.mat["glass"], **p)
                make(f"WIN_{o['id']}_frame", frame, col, c.mat["frame"], **p)
                out["windows"] += 1
            else:
                leaf = ([], [])
                add_box(leaf, (sx, sy), u, a0 + 0.01, a1 - 0.01, -0.02, 0.02, z0, z0 + h)
                make(f"DOOR_{o['id']}", leaf, col, c.mat["door"], **p)
                out["doors"] += 1
    return out


def level_rect(spec, lid, walls_only=False):
    """Bounding rectangle (x0,y0,x1,y1) of a level's walls (incl. thickness), else of its spaces."""
    pts, d = [], spec["settings"]["default_wall_thickness_m"]
    for w in spec["walls"]:
        if w["level"] == lid:
            h = (w.get("thickness_m") or d) / 2
            pts += [(p[0] + sx * h, p[1] + sy * h) for p in (w["start"], w["end"]) for sx in (-1, 1) for sy in (-1, 1)]
    if not pts and not walls_only:
        pts = [tuple(p) for s in spec["spaces"] if s["level"] == lid for p in s.get("polygon") or []]
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def build_slabs_and_floors(c, spec, lv):
    st, floors, slabs, space_ids = spec["settings"], 0, 0, {k: [] for k in lv}
    for lid, lvl in lv.items():
        z = lvl["elevation_m"]
        spaces = [s for s in spec["spaces"] if s["level"] == lid and len(s.get("polygon") or []) >= 3]
        polys = [s["polygon"] for s in spaces]
        if not polys and (r := level_rect(spec, lid)):
            polys = [rect(*r)]
        g = ([], [])
        for p in polys:
            add_prism(g, p, z - st["slab_thickness_m"], z)
        if make(f"SLAB_{lid}", g, c.col[lid]["Floors"], c.mat["slab"], level=lid):
            slabs += 1
        else:
            notes.append(f"level {lid}: no space polygons or walls, no slab")
        for s in spaces:
            g = ([], [])
            add_prism(g, s["polygon"], z + 0.001, z + 0.011)
            nm = re.sub(r"\W+", "_", s["name"]).strip("_")
            mat = c.mat["floor_wet" if s["type"] in WET else "floor_dry"]
            make(f"FLOOR_{s['id']}_{nm}", g, c.col[lid]["Floors"], mat, spec_id=s["id"], level=lid,
                 room_name=s["name"], room_type=s["type"], area_m2=float(s.get("area_m2") or 0))
            space_ids[lid].append(s["id"])
            floors += 1
    return slabs, floors, space_ids


def build_stairs(c, spec, lv, order):
    steps = 0
    for lid in order[:-1]:  # stairs rise to the next level, so none on the top one
        lvl = lv[lid]
        for s in spec["spaces"]:
            if s["level"] != lid or s["type"] != "stair" or len(s.get("polygon") or []) < 3:
                continue
            xs, ys = [p[0] for p in s["polygon"]], [p[1] for p in s["polygon"]]
            x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
            xlong = (x1 - x0) >= (y1 - y0)
            long_, short = (x1 - x0, y1 - y0) if xlong else (y1 - y0, x1 - x0)
            o, u = ((x0, y0), (1, 0)) if xlong else ((x1, y0), (0, 1))
            land = min(1.0, 0.3 * long_)
            flight = long_ - land
            n = max(2, round(lvl["h"] / 0.175))
            n1 = math.ceil(n / 2)
            n2 = n - n1
            rise, z0, half = lvl["h"] / n, lvl["elevation_m"], short / 2
            col, p = c.col[lid]["Stairs"], dict(spec_id=s["id"], level=lid)
            g = ([], [])
            add_box(g, o, u, flight, long_, 0, short, z0, z0 + n1 * rise)
            make(f"STAIR_{s['id']}_landing", g, col, c.mat["stair"], **p)
            k = 0
            for i in range(1, n1 + 1):  # flight 1 climbs away from the start, first half of the width
                k += 1
                g = ([], [])
                add_box(g, o, u, (i - 1) * flight / n1, i * flight / n1, 0, half, z0, z0 + i * rise)
                make(f"STAIR_{s['id']}_step_{k}", g, col, c.mat["stair"], **p)
            for j in range(1, n2 + 1):  # flight 2 returns, second half of the width
                k += 1
                g = ([], [])
                add_box(g, o, u, flight - j * flight / n2, flight - (j - 1) * flight / n2, half, short,
                        z0, z0 + (n1 + j) * rise)
                make(f"STAIR_{s['id']}_step_{k}", g, col, c.mat["stair"], **p)
            steps += k
    if steps:
        notes.append("stairs are indicative (dog-leg from level height, not detailed design)")
    return steps


def build_roof(c, spec, lv, order):
    r, top = spec["settings"]["roof"], lv[order[-1]]
    if r.get("type", "flat") != "flat":
        notes.append(f"roof type {r['type']} not modelled yet, flat roof used")
    rc = level_rect(spec, order[-1])
    if rc is None:
        notes.append("no footprint on the top level; no roof")
        return 0
    ov, th, par = r.get("overhang_m", 0.3), r.get("thickness_m", 0.25), r.get("parapet_m") or 0.0
    x0, y0, x1, y1 = rc[0] - ov, rc[1] - ov, rc[2] + ov, rc[3] + ov
    zb = top["elevation_m"] + top["h"]
    g = ([], [])
    add_prism(g, rect(x0, y0, x1, y1), zb, zb + th)
    if par > 0:
        t = 0.15
        for o, u, ln in (((x0, y0), (1, 0), x1 - x0), ((x1, y0), (0, 1), y1 - y0),
                         ((x1, y1), (-1, 0), x1 - x0), ((x0, y1), (0, -1), y1 - y0)):
            add_box(g, o, u, 0, ln, -t, 0, zb + th, zb + th + par)  # right-hand strip = inside edge
    make("ROOF", g, c.root_coll, c.mat["roof"], level=order[-1])
    notes.append("roof: bounding rectangle of the top level's walls grown by the overhang")
    return 1


# ---------------------------------------------------------------- scene, outputs
def mesh_bbox(skip=("SITE_GROUND",), pred=lambda ob: True):
    pts = [ob.matrix_world @ v.co for ob in bpy.context.scene.objects
           if ob.type == "MESH" and ob.name not in skip and pred(ob) for v in ob.data.vertices]
    if not pts:
        return [0, 0, 0], [0, 0, 0]
    return [min(p[i] for p in pts) for i in range(3)], [max(p[i] for p in pts) for i in range(3)]


def setup_scene(c, lo, hi):
    sc = bpy.context.scene
    sc.unit_settings.system, sc.unit_settings.scale_length = "METRIC", 1.0
    g = ([], [])
    add_prism(g, rect(lo[0] - 10, lo[1] - 10, hi[0] + 10, hi[1] + 10), -0.06, -0.01)
    make("SITE_GROUND", g, c.site_coll, material("M_Ground", (0.25, 0.38, 0.2), 0.95))
    sun = bpy.data.objects.new("SUN", bpy.data.lights.new("SUN", "SUN"))
    # exposure tuned so a 0.5 grey reads as mid-grey under the Standard view transform
    # (energy 4 + full-strength sky rendered grey concrete as near-white)
    sun.data.energy = 2.2
    sun.rotation_euler = (0.9, 0, 0.6)
    sc.collection.objects.link(sun)
    world = bpy.data.worlds.new("World")
    world.use_nodes = True
    bg = next(n for n in world.node_tree.nodes if n.type == "BACKGROUND")
    bg.inputs[0].default_value = (0.55, 0.7, 0.9, 1)
    bg.inputs[1].default_value = 0.4  # sky as soft fill light, not a second sun
    sc.world = world
    centre = (Vector(lo) + Vector(hi)) / 2
    diag = (Vector(hi) - Vector(lo)).length
    d = Vector((math.cos(math.radians(35)) * math.cos(math.radians(-45)),
                math.cos(math.radians(35)) * math.sin(math.radians(-45)), math.sin(math.radians(35))))
    cam = bpy.data.objects.new("CAM_Exterior", bpy.data.cameras.new("CAM_Exterior"))
    cam.data.lens, cam.data.clip_end = 35, 1000
    cam.location = centre + d * diag * 1.7
    cam.rotation_euler = (centre - cam.location).to_track_quat("-Z", "Y").to_euler()
    sc.collection.objects.link(cam)
    sc.camera = cam
    sc.render.resolution_x, sc.render.resolution_y, sc.render.resolution_percentage = 1280, 800, 100
    sc.render.image_settings.file_format = "PNG"
    sc.view_settings.view_transform = "Standard"


def render(png):
    sc = bpy.context.scene
    sc.render.filepath = str(png)
    for eng in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT", "BLENDER_WORKBENCH"):
        try:
            sc.render.engine = eng
            break
        except TypeError:  # the enum under-reports engines; assignment tells the truth
            continue
    try:
        sc.eevee.taa_render_samples = 16
    except AttributeError:
        pass
    try:
        bpy.ops.render.render(write_still=True)
    except Exception as e:  # EEVEE needs a GPU context; fall back to workbench
        notes.append(f"render with {sc.render.engine} failed ({e}); workbench used")
        sc.render.engine = "BLENDER_WORKBENCH"
        bpy.ops.render.render(write_still=True)


def reimport(glb):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(glb))
    bpy.context.view_layer.update()
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    lo, hi = mesh_bbox()
    return {"meshes": len(meshes), "vertices": sum(len(o.data.vertices) for o in meshes),
            "bbox_min": lo, "bbox_max": hi, "spec_ids": sorted({o["spec_id"] for o in meshes if "spec_id" in o})}


def main():
    t_start = time.time()
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if len(args) != 3:
        fail("usage: -- <input.json> <out_dir> <stem>")
    src, out, stem = Path(args[0]), Path(args[1]), args[2]
    if not src.is_file():
        fail(f"input file not found: {src}")
    try:
        spec = json.loads(src.read_text(encoding="utf-8"))
        levels = sorted(spec["levels"], key=lambda l: l["elevation_m"])
        for k in ("spaces", "walls", "openings"):
            spec.setdefault(k, [])
        st = spec["settings"] = {"slab_thickness_m": 0.2, "default_wall_thickness_m": 0.2, "default_level_height_m": 3.0,
                                 "door_height_m": 2.1, "window_sill_m": 0.9, "window_height_m": 1.2,
                                 **spec.get("settings", {})}
        st["roof"] = {"type": "flat", "thickness_m": 0.25, "overhang_m": 0.3, "parapet_m": 0.0, **st.get("roof", {})}
    except (ValueError, KeyError, TypeError) as e:
        fail(f"bad input json {src}: {e!r}")
    if not levels:
        fail("input has no levels")
    lv = {l["id"]: {**l, "h": l.get("height_m") or st["default_level_height_m"]} for l in levels}
    for e in spec["walls"] + spec["spaces"] + spec["openings"]:
        if e["level"] not in lv:
            fail(f"element {e['id']} references unknown level {e['level']}")
    out.mkdir(parents=True, exist_ok=True)

    bpy.ops.wm.read_factory_settings(use_empty=True)
    c, order = Ctx(), [l["id"] for l in levels]
    colours = {"exterior_wall": (.85, .82, .75), "interior_wall": (.92, .92, .9), "slab": (.5, .5, .48),
               "roof": (.15, .16, .17), "glass": (.4, .6, .8), "frame": (.2, .2, .2), "door": (.45, .3, .2),
               "floor_dry": (.7, .6, .45), "floor_wet": (.6, .7, .75), "stair": (.6, .6, .6)}
    colours.update(spec.get("materials", {}))
    c.mat = {k: material("M_" + k.title().replace("_", ""), v, 0.5 if k in ("frame", "door") else 0.7, glass=k == "glass")
             for k, v in colours.items()}
    c.col = {}
    for l in levels:
        root = collection(f"{l['id']} {l['name']}")
        c.col[l["id"]] = {k: collection(f"{l['id']} {k}", root) for k in ("Walls", "Openings", "Floors", "Stairs")}
    c.root_coll, c.site_coll = collection("Roof"), collection("Site")

    w = build_walls(c, spec, lv)
    slabs, floors, space_ids = build_slabs_and_floors(c, spec, lv)
    steps = build_stairs(c, spec, lv, order)
    roof = build_roof(c, spec, lv, order)

    lo, hi = mesh_bbox()
    by_level = {}
    for lid in order:
        lo_l, hi_l = mesh_bbox(skip=("SITE_GROUND", "ROOF"), pred=lambda ob, lid=lid: ob.get("level") == lid)
        by_level[lid] = {"z_min": lo_l[2], "z_max": hi_l[2], "walls": [x["id"] for x in spec["walls"] if x["level"] == lid and x["id"] in w["built"]],
                         "openings": sorted({o.get("spec_id") for o in bpy.data.objects if o.get("level") == lid and o.name.startswith(("WIN_", "DOOR_"))}),
                         "spaces": space_ids[lid]}
    setup_scene(c, lo, hi)

    blend, glb, png = out / f"{stem}.blend", out / f"{stem}.glb", out / f"{stem}_preview.png"
    bpy.ops.wm.save_as_mainfile(filepath=str(blend), compress=False)
    render(png)
    bpy.ops.export_scene.gltf(filepath=str(glb), export_format="GLB", export_apply=True, export_extras=True)
    report = {"ok": True, "blender": bpy.app.version_string,
              "objects": {"walls_pieces": w["pieces"], "walls_built": w["built"], "windows": w["windows"],
                          "doors": w["doors"], "floors": floors, "slabs": slabs, "stair_steps": steps, "roof": roof},
              "bbox_min": lo, "bbox_max": hi, "by_level": by_level,
              "glb": {"bytes": glb.stat().st_size}, "notes": notes}
    report["glb"]["reimport"] = reimport(glb)  # last: wipes the scene
    report["seconds"] = round(time.time() - t_start, 2)
    (out / f"{stem}_report.json").write_text(json.dumps(report, indent=1))
    print("RESULT", out / f"{stem}_report.json")


try:
    main()
except SystemExit:
    raise
except Exception:
    traceback.print_exc()
    sys.exit("build_model error: unexpected failure (see traceback)")
