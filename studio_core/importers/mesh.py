"""Importer for an existing 3D model (glb gltf fbx obj stl usd usdz usdc blend), via headless Blender.

engines/blender/inspect_mesh.py loads the model and reports bounding box, convex hull of the plan,
floor-slab heights and an isometric preview PNG; this module turns that into SpecGeometry.

Options: storey_height_m (default 3.0), unit_scale (metres per file unit, overrides detection).

Known limits
  * Only the outer convex hull is kept: one exterior wall per hull edge on L0, thickness unknown.
    Courtyards, indentations and every interior wall are lost. No rooms, doors or windows.
  * Everything in the file counts, including ground planes, trees and furniture, so the hull can be
    larger than the building. Delete those in the source model first.
  * Units: glb/gltf/usd* are metres by specification. fbx/obj/stl/blend carry no reliable unit, so
    they are assumed metres (millimetres if the model is over 500 units) and marked "estimated".
  * Levels come from large horizontal faces >= 2.2 m apart (topmost one is taken as the roof), else
    from total height / storey_height_m (estimated). Slab detection runs in file units.
"""
import json
import subprocess
import tempfile
from pathlib import Path

from .. import config, geometry
from ..projects import StudioError

SCRIPT = Path(__file__).resolve().parents[2] / "engines" / "blender" / "inspect_mesh.py"
METRIC_FORMATS = {".glb", ".gltf", ".usd", ".usdz", ".usdc"}
MIN_STOREY_GAP = 2.2


def _inspect(path: Path, out_json: Path) -> dict:
    cfg = config.load()["blender"]
    exe = cfg.get("executable")
    if not exe or not Path(exe).is_file():
        raise StudioError("Blender is not configured. Set blender.executable in config/studio.yaml "
                          "(or the BLENDER_EXE environment variable) to the path of blender.exe.")
    cmd = [exe, "--background", "--factory-startup", "--python", str(SCRIPT), "--", str(path), str(out_json)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=cfg.get("timeout_seconds", 1800))
    except subprocess.TimeoutExpired as e:
        raise StudioError(f"Blender timed out inspecting {path.name}") from e
    if r.returncode != 0 or not out_json.exists():
        tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-6:])
        raise StudioError(f"Blender could not inspect {path.name}:\n{tail}")
    return json.loads(out_json.read_text(encoding="utf-8"))


def _units(ext: str, raw: dict, opts: dict, notes: list[str]) -> tuple[float, str]:
    if opts.get("unit_scale"):
        notes.append(f"unit scale {opts['unit_scale']} m per file unit given by the user")
        return float(opts["unit_scale"]), "measured"
    if ext in METRIC_FORMATS:
        return 1.0, "measured"
    biggest = max(h - l for h, l in zip(raw["bbox_max"], raw["bbox_min"]))
    if biggest > 500:
        notes.append(f"largest dimension is {biggest:.0f} file units; assumed millimetres (x0.001). "
                     "Pass unit_scale to correct this.")
        return 0.001, "estimated"
    if biggest < 1:
        notes.append(f"largest dimension is only {biggest:.3g} file units; the model may be scaled down "
                     "and is NOT rescaled. Pass unit_scale to correct this.")
    else:
        notes.append(f"{ext} has no reliable unit; assumed metres. Pass unit_scale to correct this.")
    if raw.get("unit_scale") not in (None, 1.0):
        notes.append(f"the file's own scene unit scale is {raw['unit_scale']}")
    return 1.0, "estimated"


def _levels(raw: dict, f: float, scale_prov: str, storey: float, notes: list[str]):
    zlo, zhi = raw["bbox_min"][2] * f, raw["bbox_max"][2] * f
    groups = []
    for z in sorted(zz * f for zz in raw["floor_z_levels"]):
        if not groups or z - groups[-1] >= MIN_STOREY_GAP:
            groups.append(z)
    floors = groups[:-1] if len(groups) > 1 else []  # topmost horizontal surface = roof
    if len(floors) >= 2:
        notes.append(f"{len(floors)} floor levels found from horizontal slabs at z = "
                     + ", ".join(f"{z:.2f}" for z in floors) + " m; the topmost surface is taken as the roof")
        ends = floors[1:] + [groups[-1]]
        return [geometry.Level(id=f"L{i}", name=("Ground floor" if i == 0 else f"Level {i}"),
                               elevation_m=round(z - floors[0], 3), height_m=round(e - z, 3),
                               provenance=scale_prov) for i, (z, e) in enumerate(zip(floors, ends))], floors[0]
    height = zhi - zlo
    n = max(1, round(height / storey))
    notes.append(f"no distinct floor slabs found; {n} level(s) assumed from total height {height:.2f} m "
                 f"and storey height {storey} m")
    return [geometry.Level(id=f"L{i}", name=("Ground floor" if i == 0 else f"Level {i}"),
                           elevation_m=round(i * storey, 3), height_m=storey, provenance="estimated")
            for i in range(n)], zlo


def run(path: Path, **opts) -> tuple[geometry.SpecGeometry, list[str]]:
    path = Path(path)
    storey = float(opts.get("storey_height_m") or 3.0)
    notes: list[str] = []
    preview = path.with_name(path.stem + ".preview.png")
    with tempfile.TemporaryDirectory() as td:
        raw = _inspect(path, Path(td) / "inspect.json")
        tmp_png = Path(raw["preview"])
        if tmp_png.exists():
            preview.write_bytes(tmp_png.read_bytes())
    f, prov = _units(path.suffix.lower(), raw, opts, notes)
    hull = [(x * f, y * f) for x, y in raw["hull_xy"]]
    if len(hull) < 3:
        raise StudioError("the model has no usable footprint (flat or a single line)")
    x0, y0 = min(p[0] for p in hull), min(p[1] for p in hull)
    hull = [(round(x - x0, 3), round(y - y0, 3)) for x, y in hull]
    levels, _ = _levels(raw, f, prov, storey, notes)
    walls = [geometry.Wall(id=f"W{i + 1:03d}", level="L0", start=hull[i], end=hull[(i + 1) % len(hull)],
                           kind="exterior", provenance=prov) for i in range(len(hull))
             if (hull[i][0] - hull[(i + 1) % len(hull)][0]) ** 2 + (hull[i][1] - hull[(i + 1) % len(hull)][1]) ** 2 > 1e-4]
    notes += [f"{len(walls)} exterior walls from the convex hull of the whole model: courtyards and "
              "indentations are lost, interior walls, rooms, doors and windows are not extracted",
              f"{raw['object_count']} mesh objects, {raw['vertex_count']} vertices"]
    if raw.get("ground_objects"):
        notes.append("left out of the footprint as ground/terrain: " + ", ".join(raw["ground_objects"]))
    if prov == "estimated":
        notes.append("scale is estimated, so every dimension is too")
    geo = geometry.SpecGeometry(levels=levels, walls=walls, source={
        "units": "m", "scale": {"m_per_unit": f, "provenance": prov},
        "preview": preview.as_posix() if preview.exists() else None,
        "objects": raw["object_count"], "vertices": raw["vertex_count"]})
    return geo, notes
