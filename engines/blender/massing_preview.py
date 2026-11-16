"""Massing smoke test: box building -> .blend + PNG render + GLB.

Ported from the v0.1 scaffold's build_parametric.py with its Blender 5.x bugs fixed.
It takes plain numbers, NOT model_spec.json: the real spec-driven generator is Phase 4.

  blender --background --python massing_preview.py -- <out_dir> [width] [length] [floors]
"""
import sys
from pathlib import Path

import bpy
from mathutils import Vector


def material(name, rgb, roughness=0.7):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    bsdf = next(n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    bsdf.inputs["Base Color"].default_value = (*rgb, 1)
    bsdf.inputs["Roughness"].default_value = roughness
    return m


def box(name, centre, size, mat):
    bpy.ops.mesh.primitive_cube_add(size=1, location=centre)
    o = bpy.context.object
    o.name, o.dimensions = name, size
    bpy.ops.object.transform_apply(scale=True)
    o.data.materials.append(mat)
    return o


def main():
    args = sys.argv[sys.argv.index("--") + 1:]
    out = Path(args[0]).resolve()
    w, l, floors = float(args[1]) if len(args) > 1 else 10.0, float(args[2]) if len(args) > 2 else 8.0, \
        int(args[3]) if len(args) > 3 else 2
    fh, wt, st = 3.0, 0.2, 0.2
    out.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)

    wall, slab = material("M_Wall", (0.85, 0.82, 0.75)), material("M_Concrete", (0.5, 0.5, 0.48))
    roof, glass = material("M_Roof", (0.15, 0.16, 0.17)), material("M_Glass", (0.05, 0.09, 0.13), 0.1)
    ground = material("M_Ground", (0.25, 0.38, 0.2))

    box("SITE_GROUND", (0, 0, -0.05), (w + 16, l + 16, 0.1), ground)
    box("SLAB_L0", (0, 0, st / 2), (w, l, st), slab)
    for f in range(floors):
        z = st + f * fh
        box(f"WALL_L{f}_N", (0, l / 2 - wt / 2, z + fh / 2), (w, wt, fh), wall)
        box(f"WALL_L{f}_S", (0, -l / 2 + wt / 2, z + fh / 2), (w, wt, fh), wall)
        box(f"WALL_L{f}_E", (w / 2 - wt / 2, 0, z + fh / 2), (wt, l - 2 * wt, fh), wall)
        box(f"WALL_L{f}_W", (-w / 2 + wt / 2, 0, z + fh / 2), (wt, l - 2 * wt, fh), wall)
        if f:
            box(f"SLAB_L{f}", (0, 0, z), (w + 0.6, l + 0.6, st), slab)
        for i, x in enumerate((-w / 4, w / 4)):  # two windows per floor on the south and east faces
            box(f"WINDOW_L{f}_S{i}", (x, -l / 2, z + 1.6), (w / 4, 0.06, 1.5), glass)
            box(f"WINDOW_L{f}_E{i}", (w / 2, x * l / w, z + 1.6), (0.06, l / 4, 1.5), glass)
    box("DOOR_L0_S", (0, -l / 2, st + 1.05), (1.0, 0.08, 2.1), roof)
    top = st + floors * fh
    box("ROOF_MAIN", (0, 0, top + 0.1), (w + 1.0, l + 1.0, 0.2), roof)

    bpy.ops.object.light_add(type="SUN", location=(0, 0, 20), rotation=(0.9, 0, 0.6))
    bpy.context.object.data.energy = 4
    world = bpy.data.worlds.new("World")
    world.use_nodes = True
    next(n for n in world.node_tree.nodes if n.type == "BACKGROUND").inputs[0].default_value = (0.55, 0.7, 0.9, 1)
    scene = bpy.context.scene
    scene.world = world
    bpy.ops.object.camera_add(location=(w * 1.5, -l * 2.0, top * 0.9))
    cam = bpy.context.object
    cam.name = "CAM_Exterior"
    cam.rotation_euler = (Vector((0, 0, top / 2)) - cam.location).to_track_quat("-Z", "Y").to_euler()
    cam.data.lens = 28
    scene.camera = cam

    # scaffold bug: it hardcoded "BLENDER_EEVEE_NEXT", which Blender 5.x no longer has
    engines = [i.identifier for i in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items]
    scene.render.engine = next(e for e in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE", "CYCLES") if e in engines)
    scene.render.resolution_x, scene.render.resolution_y = 1280, 960
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = str(out / "massing_preview.png")

    bpy.ops.wm.save_as_mainfile(filepath=str(out / "massing_preview.blend"))
    bpy.ops.render.render(write_still=True)
    bpy.ops.export_scene.gltf(filepath=str(out / "massing_preview.glb"), export_format="GLB")
    for f in ("massing_preview.blend", "massing_preview.png", "massing_preview.glb"):
        size = (out / f).stat().st_size if (out / f).exists() else 0
        print(f"RESULT {f} {size}")
        if not size:
            raise SystemExit(f"missing output: {f}")


main()
