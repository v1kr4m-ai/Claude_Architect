"""Cutaway renders of a built model: roof hidden, one image per floor showing that floor and
everything below it. Opens the .blend read-only (never saves it).

  blender --background --factory-startup --python render_cutaway.py -- <model.blend> <out_dir> <stem>
Writes <out_dir>/<stem>_cutaway_<level>.png and prints RESULT lines.
"""
import sys
from pathlib import Path

import bpy
from mathutils import Vector


def main():
    blend, out, stem = sys.argv[sys.argv.index("--") + 1:][:3]
    bpy.ops.wm.open_mainfile(filepath=blend)
    scene = bpy.context.scene
    scene.render.resolution_x, scene.render.resolution_y = 1280, 900
    levels = sorted({str(o["level"]) for o in scene.objects if o.get("level") is not None},
                    key=lambda l: int(l[1:]) if l[1:].isdigit() else 0)
    meshes = [o for o in scene.objects if o.type == "MESH" and not o.name.startswith(("SITE", "ROOF"))]
    pts = [o.matrix_world @ Vector(c) for o in meshes for c in o.bound_box]
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    size = max(hi.x - lo.x, hi.y - lo.y)
    cam = scene.camera
    for i, lvl in enumerate(levels):
        shown = set(levels[:i + 1])
        tops = [(o.matrix_world @ Vector(c)).z for o in meshes if str(o.get("level")) in shown for c in o.bound_box]
        for ob in scene.objects:
            lv = ob.get("level")
            hide = ob.name.startswith("ROOF") or (lv is not None and str(lv) not in shown)
            ob.hide_render = ob.hide_viewport = hide
        target = Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, (max(tops) if tops else 3) * 0.4))
        cam.location = target + Vector((0.6, -0.8, 1.05)).normalized() * size * 1.55
        cam.rotation_euler = (target - cam.location).to_track_quat("-Z", "Y").to_euler()
        cam.data.lens = 30
        scene.render.filepath = str(Path(out) / f"{stem}_cutaway_{lvl}.png")
        bpy.ops.render.render(write_still=True)
        print("RESULT", scene.render.filepath)


try:
    main()
except Exception as e:  # Blender exits 0 on uncaught errors; make failures visible
    print(f"render_cutaway error: {e}", file=sys.stderr)
    sys.exit(1)
