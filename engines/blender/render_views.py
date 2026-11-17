"""Headless presentation renders of a built model (Blender 5.x). The .blend is opened and NEVER saved.

  blender --background --python render_views.py -- <model.blend> <out_dir> <job.json>

Renders, per job.json: exterior stills (front / corner / aerial), one still per interior space, an
optional line pass (`<name>_lines.png`, black edges on white, same camera) per still, and an optional
walkthrough (orbit or path) as PNG frames. Lighting presets (day / golden / overcast / dusk) replace the
scene's sun and world. Prints `RESULT <path>` per file and writes `<out_dir>/<stem>_render_report.json`.

Known limits: sun azimuth is compass degrees + north_degrees clockwise from +Y; interiors get no extra
lights (daylight only, dusk adds one warm point light per FLOOR_*); the line pass is Freestyle + object
outline in Workbench, so it shows crease/silhouette/border edges only (no textures, no hidden edges);
walkthrough uses EEVEE (falls back to Cycles if EEVEE cannot start headless).
"""
import json
import math
import sys
import time
from pathlib import Path

import bpy
from mathutils import Vector

notes = []
PRESETS = {  # sun elevation / compass azimuth (deg), sun W/m2, sky strength, warm interior lights, exposure
    # physical sky strength 1.0 is real daylight and washed everything to white (measured mean 222/255);
    # the sky is a fill light here, the sun lamp gives direct light and shadows
    "day": dict(el=55, az=225, sun=3.0, sky=0.15, lights=False, exposure=-0.3),
    "golden": dict(el=12, az=270, sun=4.0, sky=0.1, lights=False, exposure=0.0),
    "overcast": dict(el=60, az=225, sun=0.4, sky=1.0, lights=False, exposure=0.0),
    "dusk": dict(el=-2, az=270, sun=0.0, sky=1.0, lights=True, exposure=0.5),
}

INTERIOR_EV = {"day": 1.5, "golden": 3.0, "overcast": 2.0, "dusk": 1.0}  # exposure boost for interior shots


def scene():
    return bpy.context.scene


def building_bbox():
    pts = [o.matrix_world @ Vector(c) for o in scene().objects
           if o.type == "MESH" and o.name not in ("SITE_GROUND", "RENDER_GROUND") for c in o.bound_box]
    if not pts:
        raise RuntimeError("no mesh objects in the model")
    return (Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts))),
            Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts))))


def set_enum(obj, prop, candidates):
    """Assign the first candidate Blender accepts (enum lists under-report in 5.x); return it or None."""
    for c in candidates:
        try:
            setattr(obj, prop, c)
            return c
        except (TypeError, ValueError):
            continue
    return None


# ---------------------------------------------------------------- lighting
def sun_direction(el, az, north):
    a = math.radians(az + north)
    return Vector((math.sin(a) * math.cos(math.radians(el)), math.cos(a) * math.cos(math.radians(el)),
                   math.sin(math.radians(el))))


def make_world(p, north):
    world = bpy.data.worlds.new("W_Render")
    world.use_nodes = True
    nt = world.node_tree
    bg = next(n for n in nt.nodes if n.type == "BACKGROUND")
    out = next(n for n in nt.nodes if n.type == "OUTPUT_WORLD")
    bg.inputs[1].default_value = p["sky"]
    sky = None
    if p is not PRESETS["overcast"]:
        try:
            sky = nt.nodes.new("ShaderNodeTexSky")
            kind = set_enum(sky, "sky_type", ("MULTIPLE_SCATTERING", "SINGLE_SCATTERING", "NISHITA", "HOSEK_WILKIE"))
            sky.sun_disc = False  # the sun lamp supplies the sun; the disc would double it
            sky.sun_elevation = math.radians(p["el"])
            # find the sun_rotation that gives our azimuth, measuring how the node maps rotation -> compass
            def az_of(r):
                sky.sun_rotation = r
                d = sky.sun_direction
                return math.degrees(math.atan2(d[0], d[1]))
            a0, a1 = az_of(0.0), az_of(1.0)
            sign = 1 if ((a1 - a0 + 180) % 360 - 180) > 0 else -1
            sky.sun_rotation = sign * math.radians((p["az"] + north - a0) % 360)
            nt.links.new(sky.outputs[0], bg.inputs[0])
            notes.append(f"sky: {kind}")
        except (RuntimeError, TypeError, AttributeError) as e:
            sky = None
            notes.append(f"sky texture unavailable ({e}); plain colour background")
    if sky is None:
        bg.inputs[0].default_value = (0.8, 0.82, 0.85, 1) if p is PRESETS["overcast"] else \
            ((0.02, 0.04, 0.1, 1) if p["el"] < 0 else (0.45, 0.6, 0.85, 1))
    nt.links.new(bg.outputs[0], out.inputs[0])
    return world


def ground_to_horizon():
    """Render-only ground so the view does not end at the edge of the site plane (never saved)."""
    site = bpy.data.objects.get("SITE_GROUND")
    mat = site.active_material if site else None
    bpy.ops.mesh.primitive_plane_add(size=2000, location=(0, 0, -0.07))
    g = bpy.context.object
    g.name = "RENDER_GROUND"
    if mat:
        g.data.materials.append(mat)


def apply_lighting(preset, north):
    sc, p = scene(), PRESETS[preset]
    for o in [o for o in sc.objects if o.type == "LIGHT"]:
        bpy.data.objects.remove(o)
    sun = bpy.data.objects.new("SUN", bpy.data.lights.new("SUN", "SUN"))
    sun.data.energy = p["sun"]
    sun.data.color = (1.0, 0.72, 0.45) if preset == "golden" else (1.0, 0.97, 0.92)  # warm low sun
    sun.data.angle = math.radians(25 if preset == "overcast" else 0.6 if preset == "golden" else 0.5)
    sun.rotation_euler = sun_direction(p["el"], p["az"], north).to_track_quat("Z", "Y").to_euler()
    sc.collection.objects.link(sun)
    sc.world = make_world(p, north)
    vs = sc.view_settings
    notes.append("view transform: " + str(set_enum(vs, "view_transform", ("AgX", "Filmic", "Standard"))))
    set_enum(vs, "look", ("None",))
    vs.exposure = p["exposure"]
    if p["lights"]:
        for fl in [o for o in sc.objects if o.name.startswith("FLOOR_")]:
            pts = [fl.matrix_world @ Vector(c) for c in fl.bound_box]
            lo = Vector((min(v.x for v in pts), min(v.y for v in pts), min(v.z for v in pts)))
            hi = Vector((max(v.x for v in pts), max(v.y for v in pts), max(v.z for v in pts)))
            ld = bpy.data.lights.new("L_" + fl.name, "POINT")
            ld.energy, ld.color, ld.shadow_soft_size = 600, (1.0, 0.64, 0.33), 0.2  # ~2700K warm
            lo_obj = bpy.data.objects.new("L_" + fl.name, ld)
            lo_obj.location = ((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, hi.z + 2.4)
            sc.collection.objects.link(lo_obj)


# ---------------------------------------------------------------- engines
def enable_gpu():
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
    except KeyError:
        return "CPU (cycles addon missing)"
    for kind in ("OPTIX", "CUDA"):
        if set_enum(prefs, "compute_device_type", (kind,)) is None:
            continue
        for fn in ("refresh_devices", "get_devices"):
            if hasattr(prefs, fn):
                getattr(prefs, fn)()
                break
        devs = [d for d in prefs.devices if d.type == kind]
        for d in prefs.devices:
            d.use = d.type == kind
        if devs:
            scene().cycles.device = "GPU"
            return f"GPU {kind}: " + ", ".join(d.name for d in devs)
    scene().cycles.device = "CPU"
    return "CPU (no OPTIX/CUDA device)"


def setup_engine(engine, samples):
    sc = scene()
    if engine == "cycles":
        sc.render.engine = "CYCLES"
        c = sc.cycles
        c.samples, c.use_adaptive_sampling, c.use_denoising = samples, True, True
        set_enum(c, "denoiser", ("OPENIMAGEDENOISE", "OPTIX"))
        return enable_gpu()
    eng = set_enum(sc.render, "engine", ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"))
    e, on = sc.eevee, []
    for prop, val in (("taa_render_samples", samples), ("use_raytracing", True), ("use_shadows", True),
                      ("use_fast_gi", True), ("shadow_ray_count", 2), ("shadow_step_count", 8)):
        try:
            setattr(e, prop, val)
            on.append(prop)
        except (AttributeError, TypeError):
            pass
    notes.append(f"eevee ({eng}) set: {', '.join(on)}")
    return "EEVEE (GPU)"


# ---------------------------------------------------------------- cameras
def new_camera():
    cam = bpy.data.objects.new("CAM_Render", bpy.data.cameras.new("CAM_Render"))
    scene().collection.objects.link(cam)
    scene().camera = cam
    cam.data.sensor_width, cam.data.sensor_fit, cam.data.clip_start, cam.data.clip_end = 36, "AUTO", 0.05, 2000
    return cam


def look_at(cam, pos, target):
    cam.location, cam.data.shift_x, cam.data.shift_y = pos, 0, 0
    cam.rotation_euler = (target - pos).to_track_quat("-Z", "Y").to_euler()


def level_cam(cam, pos, look_xy, z_centre, lens, depth=None):
    """Horizontal camera (no convergence); lens shift puts z_centre mid-frame."""
    cam.data.lens = lens
    v = Vector((look_xy[0] - pos.x, look_xy[1] - pos.y, 0))
    look_at(cam, pos, pos + v)
    cam.data.shift_y = (z_centre - pos.z) / ((depth or v.length) * cam.data.sensor_width / lens)


def fit_dist(radius, lens, aspect, margin=1.15):
    half = math.atan(18 * min(1, aspect) / lens)
    return radius * margin / math.sin(half)


def exterior_camera(cam, view, lo, hi, aspect):
    c, size = (lo + hi) / 2, hi - lo
    R = size.length / 2
    se = Vector((1, -1, 0)).normalized()
    if view == "front":
        lens = 30
        d = max(size.x * 1.2 * lens / 36, size.z * 1.25 * lens / (36 * aspect))
        level_cam(cam, Vector((c.x, lo.y - d, 1.7)), (c.x, c.y), size.z / 2, lens, depth=d + size.y / 2)
    elif view == "corner":
        d = fit_dist(R, 28, aspect)
        level_cam(cam, Vector((c.x, c.y, 0)) + se * d + Vector((0, 0, 2.2)), (c.x, c.y), c.z, 28)
    elif view == "aerial":
        cam.data.lens = 28
        d = fit_dist(R, 28, aspect, 1.5)
        el = math.radians(40)
        look_at(cam, c + se * math.cos(el) * d + Vector((0, 0, math.sin(el) * d)), Vector((c.x, c.y, size.z * 0.4)))
    else:
        raise RuntimeError(f"unknown view '{view}'")


def interior_camera(cam, sp):
    poly = sp["polygon"]
    cx, cy = sum(p[0] for p in poly) / len(poly), sum(p[1] for p in poly) / len(poly)
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    corners = [(x, y) for x in (min(xs), max(xs)) for y in (min(ys), max(ys))]
    x, y = max(corners, key=lambda q: math.hypot(q[0] - cx, q[1] - cy))
    x += 0.6 * (1 if cx > x else -1)
    y += 0.6 * (1 if cy > y else -1)
    z = float(sp.get("level_z") or 0.0) + 1.5
    cam.data.lens = 19
    look_at(cam, Vector((x, y, z)), Vector((cx, cy, z - 0.1)))


# ---------------------------------------------------------------- render helpers
def render_to(path):
    scene().render.filepath = str(path)
    bpy.ops.render.render(write_still=True)
    print("RESULT", path)


def line_mode(on, state):
    """Switch the scene to/from the flat edge-only look. `state` stores what to restore."""
    sc = scene()
    if on:
        state.update(engine=sc.render.engine, world=sc.world, vt=sc.view_settings.view_transform,
                     look=sc.view_settings.look, exp=sc.view_settings.exposure, fs=sc.render.use_freestyle,
                     dither=sc.render.dither_intensity)
        w = bpy.data.worlds.new("W_Lines")
        w.color = (1, 1, 1)
        sc.world = w
        sc.render.engine = "BLENDER_WORKBENCH"
        sh = sc.display.shading
        sh.light, sh.color_type, sh.single_color = "FLAT", "SINGLE", (1, 1, 1)
        sh.show_object_outline, sh.object_outline_color = True, (0, 0, 0)
        sh.show_shadows, sh.show_cavity = False, False
        set_enum(sc.view_settings, "view_transform", ("Standard",))
        set_enum(sc.view_settings, "look", ("None",))
        sc.view_settings.exposure, sc.render.dither_intensity = 0, 0
        sc.render.use_freestyle = True
        vl = bpy.context.view_layer
        vl.use_freestyle = True
        fs = vl.freestyle_settings
        ls = fs.linesets[0] if len(fs.linesets) else fs.linesets.new("Edges")
        ls.select_silhouette = ls.select_crease = ls.select_border = True
        ls.select_by_visibility, ls.visibility = True, "VISIBLE"
        fs.crease_angle = math.radians(120)
        ls.linestyle = ls.linestyle or bpy.data.linestyles.new("LS_Edges")
        ls.linestyle.color, ls.linestyle.thickness = (0, 0, 0), 1.6
    else:
        sc.render.engine, sc.world = state["engine"], state["world"]
        set_enum(sc.view_settings, "view_transform", (state["vt"],))
        set_enum(sc.view_settings, "look", (state["look"],))
        sc.view_settings.exposure, sc.render.use_freestyle = state["exp"], state["fs"]
        sc.render.dither_intensity = state["dither"]


def catmull(pts, n=24):
    """Sample a Catmull-Rom spline through pts; returns a dense polyline."""
    P = [pts[0]] + pts + [pts[-1]]
    out = []
    for i in range(1, len(P) - 2):
        p0, p1, p2, p3 = P[i - 1], P[i], P[i + 1], P[i + 2]
        for k in range(n):
            t = k / n
            out.append(0.5 * ((2 * p1) + (-p0 + p2) * t + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t * t
                              + (-p0 + 3 * p1 - 3 * p2 + p3) * t ** 3))
    out.append(pts[-1])
    return out


def along(poly, s):
    """Point at arc length s on a polyline (clamped at the ends) and its unit tangent."""
    acc = 0.0
    for a, b in zip(poly, poly[1:]):
        seg = (b - a).length
        if seg > 1e-9 and acc + seg >= s:
            return a.lerp(b, (s - acc) / seg), (b - a).normalized()
        acc += seg
    tang = (poly[-1] - poly[-2]).normalized()
    return poly[-1] + tang * (s - acc), tang


def walkthrough(wt, out, stem, lo, hi, cam, base_engine_samples):
    sc = scene()
    t0 = time.time()
    sc.render.resolution_x, sc.render.resolution_y = wt.get("width", 960), wt.get("height", 540)
    setup_engine("eevee", wt.get("samples", 16))
    d = Path(out) / "frames" / f"{stem}_{wt['kind']}"
    d.mkdir(parents=True, exist_ok=True)
    n, c, size = int(wt["frames"]), (lo + hi) / 2, hi - lo
    cam.data.shift_y = 0
    if wt["kind"] == "orbit":
        cam.data.lens = 28
        r = fit_dist(size.length / 2, 28, sc.render.resolution_y / sc.render.resolution_x, 1.1)
        z = 0.6 * size.z + 3
        poses = [(Vector((c.x + r * math.sin(2 * math.pi * i / n), c.y - r * math.cos(2 * math.pi * i / n), z)),
                  Vector((c.x, c.y, lo.z + size.z * 0.4))) for i in range(n)]
    elif wt["kind"] == "path":
        cam.data.lens = 18
        # a walk-through goes through doorways: leave the openings clear instead of passing the
        # camera through a closed leaf (render-only; the .blend is never saved)
        for ob in sc.objects:
            if ob.name.startswith("DOOR_"):
                ob.hide_render = True
        notes.append("walkthrough: door leaves hidden so doorways read as open")
        pts = [Vector(p) for p in wt["path"]]
        if len(pts) < 2:
            raise RuntimeError("walkthrough path needs at least 2 points")
        poly = catmull(pts)
        total = sum((b - a).length for a, b in zip(poly, poly[1:]))
        poses = []
        for i in range(n):
            s = total * i / max(n - 1, 1)
            pos, _ = along(poly, s)
            ahead, _ = along(poly, s + 3.0)
            poses.append((pos, Vector((ahead.x, ahead.y, pos.z))))  # level horizon
    else:
        raise RuntimeError(f"unknown walkthrough kind '{wt['kind']}'")
    engine_note = "eevee"
    for i, (pos, tgt) in enumerate(poses):
        if tgt.xy == pos.xy:  # degenerate look-ahead (stationary path end): keep previous heading
            tgt = pos + Vector((0, 1, 0))
        look_at(cam, pos, tgt)
        try:
            render_to(d / f"frame_{i + 1:04d}.png")
        except RuntimeError as e:
            if i:
                raise
            notes.append(f"EEVEE failed headless ({e}); walkthrough rendered with Cycles")
            setup_engine("cycles", wt.get("samples", 16))
            engine_note = "cycles"
            render_to(d / f"frame_{i + 1:04d}.png")
    return {"kind": wt["kind"], "dir": str(d), "frames": n, "fps": wt.get("fps", 24), "engine": engine_note,
            "seconds": round(time.time() - t0, 2), "seconds_per_frame": round((time.time() - t0) / n, 2)}


# ---------------------------------------------------------------- main
def main():
    t_start = time.time()
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if len(args) != 3:
        raise RuntimeError("usage: render_views.py -- <model.blend> <out_dir> <job.json>")
    blend, out, jobp = args
    if not Path(blend).is_file():
        raise RuntimeError(f"model .blend not found: {blend}")
    job = json.loads(Path(jobp).read_text(encoding="utf-8"))
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.open_mainfile(filepath=blend)
    sc, stem, preset = scene(), job.get("stem", Path(blend).stem), job.get("preset", "day")
    if preset not in PRESETS:
        raise RuntimeError(f"unknown preset '{preset}'")
    lo, hi = building_bbox()
    sc.render.resolution_x, sc.render.resolution_y, sc.render.resolution_percentage = job["width"], job["height"], 100
    sc.render.image_settings.file_format, sc.render.image_settings.color_mode = "PNG", "RGB"
    sc.render.film_transparent = False
    ground_to_horizon()
    apply_lighting(preset, float(job.get("north_degrees") or 0))
    device = setup_engine(job.get("engine", "cycles"), int(job.get("samples", 48)))
    aspect = job["height"] / job["width"]
    cam = new_camera()
    shots = [(v, lambda v=v: exterior_camera(cam, v, lo, hi, aspect)) for v in job.get("views", [])]
    shots += [(f"interior_{sp['id']}", lambda sp=sp: interior_camera(cam, sp)) for sp in job.get("interiors") or []]
    stills = []
    base_exposure = sc.view_settings.exposure
    for name, place in shots:
        t0 = time.time()
        place()
        # interiors only see daylight through windows: compensate like a photographer would
        # (measured: golden-hour interiors rendered near-black at the exterior exposure)
        sc.view_settings.exposure = base_exposure + (INTERIOR_EV.get(preset, 2.0) if name.startswith("interior_") else 0)
        f = out / f"{stem}_{preset}_{name}.png"
        render_to(f)
        stills.append({"view": name, "file": str(f), "lines": None, "seconds": round(time.time() - t0, 2)})
    sc.view_settings.exposure = base_exposure
    if job.get("lines") and shots:
        state = {}
        line_mode(True, state)
        for (name, place), st in zip(shots, stills):
            t0 = time.time()
            place()
            lf = out / (Path(st["file"]).stem + "_lines.png")
            render_to(lf)
            st["lines"], st["lines_seconds"] = str(lf), round(time.time() - t0, 2)
        line_mode(False, state)
    wt = None
    if job.get("walkthrough"):
        wt = walkthrough(job["walkthrough"], out, stem, lo, hi, cam, job.get("samples", 48))
    rep = {"ok": True, "blender": bpy.app.version_string, "preset": preset, "engine": job.get("engine", "cycles"),
           "device": device, "stills": stills, "walkthrough": wt, "notes": notes,
           "seconds": round(time.time() - t_start, 2)}
    rp = out / f"{stem}_render_report.json"
    rp.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    print("RESULT", rp)


try:
    main()
except Exception as e:  # Blender exits 0 on uncaught errors; make failures visible
    print(f"render_views error: {e}", file=sys.stderr)
    sys.exit(1)
