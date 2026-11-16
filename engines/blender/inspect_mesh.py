"""Inspect an existing 3D model headless and write facts about it as JSON, plus a preview PNG.

  blender --background --factory-startup --python inspect_mesh.py -- <model> <out.json>

Writes <out.json> and <out>.png (isometric workbench render, for later vision analysis).
All numbers are in the file's own units (before any unit conversion; studio_core decides that).
"""
import json
import sys
from pathlib import Path

import bpy
import numpy as np
from mathutils import Vector, geometry

HULL_SAMPLE = 200_000
SLAB_MIN_AREA = 2.0


def import_model(path: Path):
    ext = path.suffix.lower()
    f = str(path)
    if ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=f)
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=f)
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=f)
    elif ext == ".stl":
        bpy.ops.wm.stl_import(filepath=f)
    elif ext in (".usd", ".usdz", ".usdc"):
        bpy.ops.wm.usd_import(filepath=f)
    elif ext == ".blend":
        bpy.ops.wm.open_mainfile(filepath=f)
    else:
        raise SystemExit(f"unsupported extension {ext}")


def collect():
    dg = bpy.context.evaluated_depsgraph_get()
    pts, floors, objs, names = [], [], 0, []
    for ob in bpy.context.scene.objects:
        if ob.type != "MESH":
            continue
        ev = ob.evaluated_get(dg)
        me = ev.to_mesh()
        if not len(me.vertices):
            ev.to_mesh_clear()
            continue
        objs += 1
        names.append(ob.name)
        m = np.array(ob.matrix_world)
        co = np.empty(len(me.vertices) * 3)
        me.vertices.foreach_get("co", co)
        pts.append(co.reshape(-1, 3) @ m[:3, :3].T + m[:3, 3])
        if len(me.polygons):
            n = np.empty(len(me.polygons) * 3)
            a = np.empty(len(me.polygons))
            c = np.empty(len(me.polygons) * 3)
            me.polygons.foreach_get("normal", n)
            me.polygons.foreach_get("area", a)
            me.polygons.foreach_get("center", c)
            n, c = n.reshape(-1, 3) @ m[:3, :3].T, c.reshape(-1, 3) @ m[:3, :3].T + m[:3, 3]
            n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
            s = abs(np.linalg.det(m[:3, :3])) ** (1 / 3)  # uniform-scale area factor
            keep = (n[:, 2] > 0.95) & (a * s * s > SLAB_MIN_AREA)
            floors.append(c[keep, 2])
        ev.to_mesh_clear()
    return pts, (np.concatenate(floors) if floors else np.zeros(0)), objs, names


def split_ground(pts, names):
    """Terrain/site planes would make the footprint the size of the plot. An object counts as
    ground if it is thin, sits at the bottom of the model, and covers > 2x the plan area of
    everything else. ponytail: bbox heuristic; sloped terrain meshes taller than 0.5 units pass through."""
    if len(pts) < 2:
        return pts, []
    zmin = min(p[:, 2].min() for p in pts)
    keep, ground = [], []
    for p, n in zip(pts, names):
        thin = p[:, 2].max() - p[:, 2].min() < 0.5
        bottom = p[:, 2].min() - zmin < 0.5
        (ground if thin and bottom else keep).append((p, n))
    if not ground or not keep:
        return pts, []
    rest = np.vstack([p for p, _ in keep])
    rest_area = np.ptp(rest[:, 0]) * np.ptp(rest[:, 1]) or 1e-9
    big = {n for p, n in ground if np.ptp(p[:, 0]) * np.ptp(p[:, 1]) > 2 * rest_area}
    if not big:
        return pts, []
    return [p for p, n in zip(pts, names) if n not in big], sorted(big)


def hull_xy(p):
    if len(p) > HULL_SAMPLE:  # sample, but never lose the extremes
        ext = [p[:, 0].argmin(), p[:, 0].argmax(), p[:, 1].argmin(), p[:, 1].argmax()]
        p = np.vstack([p[:: len(p) // HULL_SAMPLE + 1], p[ext]])
    idx = geometry.convex_hull_2d([Vector((float(x), float(y))) for x, y, _ in p])
    return [[float(p[i][0]), float(p[i][1])] for i in idx]


def preview(lo, hi, out_png):
    scene = bpy.context.scene
    # the enum under-reports engines: try the likely identifiers, assignment raises TypeError if invalid
    for eng in ("BLENDER_WORKBENCH", "WORKBENCH"):
        try:
            scene.render.engine = eng
            break
        except TypeError:
            continue
    scene.render.resolution_x, scene.render.resolution_y = 800, 600
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = str(out_png)
    centre = (Vector(lo) + Vector(hi)) / 2
    size = (Vector(hi) - Vector(lo)).length or 1.0
    cam_data = bpy.data.cameras.new("preview_cam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = size * 1.1
    cam_data.clip_start, cam_data.clip_end = 0.01, size * 10
    cam = bpy.data.objects.new("preview_cam", cam_data)
    scene.collection.objects.link(cam)
    cam.location = centre + Vector((1, -1, 0.8)).normalized() * size * 2
    cam.rotation_euler = (centre - cam.location).to_track_quat("-Z", "Y").to_euler()
    scene.camera = cam
    bpy.ops.render.render(write_still=True)


def main():
    model, out = (Path(a) for a in sys.argv[sys.argv.index("--") + 1:][:2])
    if model.suffix.lower() != ".blend":
        bpy.ops.wm.read_factory_settings(use_empty=True)
    import_model(model)
    per_obj, floor_z, objs, names = collect()
    if not per_obj:
        raise SystemExit("no mesh geometry found in the model")
    building, ground = split_ground(per_obj, names)
    pts = np.vstack(building)
    lo, hi = pts.min(axis=0).tolist(), pts.max(axis=0).tolist()
    levels = sorted({round(float(z) / 0.05) * 0.05 for z in floor_z})
    png = out.with_suffix(".png")
    preview(lo, hi, png)
    out.write_text(json.dumps({
        "object_count": objs, "vertex_count": int(len(pts)),
        "bbox_min": lo, "bbox_max": hi,
        "unit_scale": bpy.context.scene.unit_settings.scale_length,
        "hull_xy": hull_xy(pts), "floor_z_levels": [round(z, 3) for z in levels],
        "preview": str(png), "ground_objects": ground}, indent=1))
    print("RESULT", out)


main()
