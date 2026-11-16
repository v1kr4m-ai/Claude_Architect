"""3D model from the approved model_spec.json (Phase 4a).

  model_spec.json + brief preferences --> engines/blender/build_model.py (headless Blender)
    --> blender/model_rNNNN.blend, output/model_rNNNN.glb, renders/model_rNNNN_preview.png
    --> independent plan-vs-model consistency checks (this module, not the Blender script)

The .blend is never overwritten: if a file for this revision exists (you may have edited it in
Blender), the next build gets a _b2, _b3 ... suffix.
"""
import json
import re
import subprocess
import time
from pathlib import Path

from . import config, projects
from .projects import StudioError

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "engines" / "blender" / "build_model.py"
CUTAWAY = ROOT / "engines" / "blender" / "render_cutaway.py"

# keyword -> linear-ish RGB used for the Principled base colour
PALETTE = [(("white", "render", "plaster", "stucco", "lime"), (0.86, 0.85, 0.82)),
           (("brick",), (0.52, 0.25, 0.17)), (("stone", "laterite", "sandstone"), (0.6, 0.55, 0.47)),
           (("wood", "timber", "teak"), (0.42, 0.27, 0.15)), (("black", "charcoal", "dark"), (0.06, 0.06, 0.07)),
           (("grey", "gray", "concrete", "cement"), (0.52, 0.52, 0.5)), (("terracotta",), (0.6, 0.3, 0.2)),
           (("beige", "sand", "cream"), (0.78, 0.72, 0.6)),
           (("metal", "steel", "aluminium", "aluminum", "zinc"), (0.25, 0.26, 0.28))]
GLAZING = ("glass", "glazing", "glazed")  # windows already use the glass material
DEFAULTS = {"exterior_wall": (0.86, 0.85, 0.82), "interior_wall": (0.9, 0.89, 0.87), "slab": (0.5, 0.5, 0.48),
            "roof": (0.35, 0.36, 0.37), "glass": (0.55, 0.7, 0.75), "frame": (0.1, 0.1, 0.11),
            "door": (0.42, 0.27, 0.15), "floor_dry": (0.62, 0.48, 0.33), "floor_wet": (0.78, 0.8, 0.8),
            "stair": (0.55, 0.55, 0.53)}


def _colour(text: str):
    t = text.lower()
    return next((rgb for words, rgb in PALETTE if any(w in t for w in words)), None)


def materials(prefs: dict) -> tuple[dict, list[str]]:
    """Map the approved brief's facade materials to colours. First recognised material = walls,
    a second one (if any) = window frames/doors accent. Unrecognised names are reported."""
    out, notes = dict(DEFAULTS), []
    names = [m for m in (prefs.get("materials") or []) if isinstance(m, str)]
    glazing = [m for m in names if any(w in m.lower() for w in GLAZING)]
    if glazing:
        notes.append(f"{', '.join(glazing)}: used for windows")
    found = [(m, _colour(m)) for m in names if m not in glazing]
    known = [(m, c) for m, c in found if c]
    if known:
        out["exterior_wall"] = known[0][1]
        notes.append(f"exterior walls: {known[0][0]}")
    if len(known) > 1:
        out["frame"] = known[1][1]
        notes.append(f"frames: {known[1][0]}")
    for m, c in found:
        if not c:
            notes.append(f"material '{m}' not recognised; default colour used")
    return {k: list(v) for k, v in out.items()}, notes


def roof_settings(prefs: dict) -> dict:
    text = str(prefs.get("roof") or "flat").lower()
    kind = next((k for k in ("gable", "hip", "shed", "mansard", "pitched", "sloped") if k in text), "flat")
    return {"type": kind, "thickness_m": 0.25, "overhang_m": 0.3 if "minimal" not in text else 0.15,
            "parapet_m": 0.9 if "parapet" in text else 0.0}


def _stem(p: Path, rev: int) -> str:
    stem, n = f"model_r{rev:04d}", 1
    while (p / "blender" / f"{stem}.blend").exists():
        n += 1
        stem = f"model_r{rev:04d}_b{n}"
    return stem


def build(slug: str) -> dict:
    cfg = config.load()
    exe = cfg["blender"]["executable"]
    if not exe or not Path(exe).is_file():
        raise StudioError("Blender is not configured: set blender.executable in config/studio.yaml")
    p = projects.project_dir(slug)
    spec = projects.read_state(slug, "model_spec.json")
    if not spec.get("walls"):
        raise StudioError("model_spec.json has no walls yet: approve a generated plan or an import first")
    meta, brief = projects.read_state(slug, "project.json"), projects.read_state(slug, "design_brief.yaml")
    rev = meta["revision"]
    prefs = brief.get("preferences") or {}
    mats, notes = materials(prefs)
    roof = roof_settings(prefs)
    inp = {"project": meta["name"], "revision": rev,
           **{k: spec.get(k, []) for k in ("levels", "spaces", "walls", "openings")},
           "settings": {"slab_thickness_m": 0.2, "default_wall_thickness_m": 0.2, "default_level_height_m": 3.0,
                        "door_height_m": 2.1, "window_sill_m": 0.9, "window_height_m": 1.2, "roof": roof},
           "materials": mats}
    for d in ("blender", "output", "renders"):
        (p / d).mkdir(exist_ok=True)
    stem = _stem(p, rev)
    work = p / "blender"
    (work / f"{stem}_input.json").write_text(json.dumps(inp, indent=1), encoding="utf-8")  # kept: reproducible
    t0 = time.monotonic()
    r = subprocess.run([exe, "--background", "--factory-startup", "--python", str(SCRIPT), "--",
                        str(work / f"{stem}_input.json"), str(work), stem],
                       capture_output=True, text=True, timeout=cfg["blender"]["timeout_seconds"])
    report_f = work / f"{stem}_report.json"
    if r.returncode != 0 or not report_f.exists():
        tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-12:])
        raise StudioError(f"Blender build failed (exit {r.returncode}):\n{tail}")
    report = json.loads(report_f.read_text(encoding="utf-8"))
    # deliverables go where people look for them; the .blend stays in blender/
    glb, png = p / "output" / f"{stem}.glb", p / "renders" / f"{stem}_preview.png"
    (work / f"{stem}.glb").replace(glb)
    (work / f"{stem}_preview.png").replace(png)
    cut = subprocess.run([exe, "--background", "--factory-startup", "--python", str(CUTAWAY), "--",
                          str(work / f"{stem}.blend"), str(p / "renders"), stem],
                         capture_output=True, text=True, timeout=cfg["blender"]["timeout_seconds"])
    cutaways = sorted(f"renders/{Path(l.split(' ', 1)[1].strip()).name}" for l in cut.stdout.splitlines()
                      if l.startswith("RESULT "))
    if cut.returncode != 0:
        notes.append("cutaway renders failed: " + (cut.stderr.strip().splitlines() or ["?"])[-1])
    checks = consistency(spec, report, roof)
    rec = {"stem": stem, "revision": rev, "seconds": round(time.monotonic() - t0, 1),
           "files": {"blend": f"blender/{stem}.blend", "glb": f"output/{stem}.glb", "preview": f"renders/{stem}_preview.png",
                     "report": f"blender/{stem}_report.json", "cutaways": cutaways},
           "checks": checks, "consistent": all(c["ok"] for c in checks),
           "notes": notes + [f"roof: {roof['type']}"] + report.get("notes", [])}
    for kind, rel in (("model_blend", rec["files"]["blend"]), ("model_glb", rec["files"]["glb"]),
                      ("model_preview", rec["files"]["preview"]), *(("model_preview", c) for c in cutaways)):
        projects.add_artifact(slug, kind, p / rel, "geometry_engineer", authoritative=rec["consistent"],
                              note=f"built from model revision r{rev}", source_revision=rev)
    (work / f"{stem}_checks.json").write_text(json.dumps(rec, indent=1), encoding="utf-8")
    return rec


def _close(a, b, tol):
    return abs(a - b) <= tol


def consistency(spec: dict, report: dict, roof: dict) -> list[dict]:
    """Does the built model match the approved spec? Computed here from the spec, so a bug in the
    Blender script cannot mark its own work as correct."""
    out = []

    def check(name, ok, detail):
        out.append({"check": name, "ok": bool(ok), "detail": detail})

    built = set(report["objects"].get("walls_built", []))
    missing = [w["id"] for w in spec["walls"] if w["id"] not in built]
    check("every wall built", not missing, f"missing: {missing}" if missing else f"{len(built)} walls")
    for kind, key in (("window", "windows"), ("door", "doors")):
        want = sum(o["kind"] == kind for o in spec.get("openings", []))
        got = report["objects"].get(key, 0)
        check(f"{key} count", want == got, f"spec {want}, model {got}")
    want_floors = sum(len(s.get("polygon") or []) >= 3 for s in spec.get("spaces", []))
    check("room floors", report["objects"].get("floors") == want_floors,
          f"spec {want_floors}, model {report['objects'].get('floors')}")

    xs = [c[0] for w in spec["walls"] for c in (w["start"], w["end"])]
    ys = [c[1] for w in spec["walls"] for c in (w["start"], w["end"])]
    t = max((w.get("thickness_m") or 0.2) for w in spec["walls"]) / 2
    plan = (min(xs) - t, min(ys) - t, max(xs) + t, max(ys) + t)
    lo, hi = report["bbox_min"], report["bbox_max"]
    slack = roof["overhang_m"] + 0.05
    inside = all(lo[i] <= plan[i] + 0.02 and hi[i] >= plan[i + 2] - 0.02 for i in (0, 1))
    tight = all(plan[i] - lo[i] <= slack and hi[i] - plan[i + 2] <= slack for i in (0, 1))
    check("model footprint matches plan", inside and tight,
          f"plan {plan[2] - plan[0]:.2f} x {plan[3] - plan[1]:.2f} m, model {hi[0] - lo[0]:.2f} x {hi[1] - lo[1]:.2f} m "
          f"(roof overhang {roof['overhang_m']} m allowed)")

    levels = sorted(spec.get("levels", []), key=lambda l: l["elevation_m"])
    if levels:
        top = levels[-1]["elevation_m"] + (levels[-1].get("height_m") or 3.0)
        check("building height", hi[2] >= top - 0.02 and hi[2] <= top + roof["thickness_m"] + roof["parapet_m"] + 0.05,
              f"top of walls {top:.2f} m, model top {hi[2]:.2f} m")
    for l in levels:
        z = report.get("by_level", {}).get(l["id"])
        if not z:
            check(f"{l['id']} present", False, "level missing from model")
            continue
        want_top = l["elevation_m"] + (l.get("height_m") or 3.0)
        check(f"{l['id']} height", _close(z["z_max"], want_top, 0.05),
              f"walls top {z['z_max']:.2f} m, spec {want_top:.2f} m")
    stairs = [s for s in spec.get("spaces", []) if s.get("type") == "stair"]
    if len(levels) > 1 and stairs:
        check("stairs built", report["objects"].get("stair_steps", 0) > 0, f"{report['objects'].get('stair_steps', 0)} steps")

    g = report.get("glb", {})
    re_ = g.get("reimport", {})
    same = re_.get("bbox_min") and all(_close(a, b, 0.05) for a, b in zip(re_["bbox_min"] + re_["bbox_max"], lo + hi))
    check("GLB re-import matches", g.get("bytes", 0) > 0 and re_.get("meshes", 0) > 0 and same,
          f"{g.get('bytes', 0)} bytes, {re_.get('meshes', 0)} meshes")
    ids = set(re_.get("spec_ids", []))
    lost = [w["id"] for w in spec["walls"] if w["id"] not in ids]
    check("GLB keeps element ids", not lost, f"{len(lost)} wall ids missing in GLB" if lost else "all wall ids present")
    return out
