"""Visualisation (Phase 5): renders, AI enhancement and walkthrough video from the built model.

  blender/model_rNNNN.blend --(engines/blender/render_views.py)--> renders/*.png (+ _lines.png)
       --(optional, ComfyUI)--> renders/*_enhanced.png  [non-authoritative, geometry-scored]
       --(optional)--> videos/*.mp4  (Blender frames assembled by ffmpeg)

Blender renders are the authoritative images. Enhanced images are kept beside them, never
replace them, and carry an edge-recall score against Blender's line pass; one below the
threshold is kept but flagged as having altered the geometry.
Heavy steps run one at a time (Blender, then ComfyUI) so an 8 GB card is never shared.
"""
import json
import subprocess
import time
from pathlib import Path

import httpx
from shapely.geometry import Polygon

from . import comfy, config, projects
from .projects import StudioError

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "engines" / "blender" / "render_views.py"
PRESETS = ("day", "golden", "overcast", "dusk")
VIEWS = ("front", "corner", "aerial")
HABITABLE = ("living", "dining", "kitchen", "master_bedroom", "bedroom", "study")


def current_model(slug: str) -> dict:
    """The latest built model, refused if the geometry changed after it was built."""
    arts = projects.read_state(slug, "artifacts.json")["artifacts"]
    blends = [a for a in arts if a["kind"] == "model_blend"]
    if not blends:
        raise StudioError("no 3D model yet: run build-model first")
    art = max(blends, key=lambda a: (a["revision"], a["at"]))
    hist = projects.read_state(slug, "revision_history.json")["revisions"]
    spec_rev = max((r["revision"] for r in hist if r.get("file") == "model_spec.json"), default=0)
    if art["revision"] < spec_rev:
        raise StudioError(f"the 3D model is from r{art['revision']} but the plan changed in r{spec_rev}: "
                          "run build-model again")
    if not (projects.project_dir(slug) / art["path"]).exists():
        raise StudioError(f"model file missing: {art['path']}")
    return art


def pick_interiors(spec: dict, n: int) -> list[dict]:
    """The n largest habitable rooms, with the floor height the camera stands on."""
    z = {l["id"]: l["elevation_m"] for l in spec.get("levels", [])}
    rooms = [s for s in spec.get("spaces", []) if s.get("type") in HABITABLE and len(s.get("polygon") or []) >= 3]
    rooms.sort(key=lambda s: -(s.get("area_m2") or 0))
    return [{"id": s["id"], "name": s["name"], "type": s["type"], "level_z": z.get(s["level"], 0.0),
             "polygon": s["polygon"]} for s in rooms[:n]]


def walk_path(spec: dict) -> list[list[float]] | None:
    """Approach the front door, enter, and walk the ground-floor hall. None if the plan has no
    recognisable front door + hall (e.g. an imported shell)."""
    lvl = min(spec.get("levels", []), key=lambda l: l["elevation_m"], default=None)
    if not lvl:
        return None
    doors = [o for o in spec.get("openings", []) if o["kind"] == "door" and o["level"] == lvl["id"]]
    halls = [s for s in spec.get("spaces", []) if s["level"] == lvl["id"] and s.get("type") in ("entrance", "corridor")
             and len(s.get("polygon") or []) >= 3]
    if not doors or not halls:
        return None
    hall = max(halls, key=lambda s: s.get("area_m2") or 0)
    poly = Polygon(hall["polygon"])
    front = min(doors, key=lambda d: (poly.exterior.distance(Polygon([d["position"]] * 3).centroid),
                                      -(d.get("width_m") or 0)))
    dx, dy = front["position"]
    cx, cy = poly.centroid.x, poly.centroid.y
    # unit vector pointing from the door into the hall; the approach starts on the opposite side
    vx, vy = cx - dx, cy - dy
    n = (vx * vx + vy * vy) ** 0.5 or 1.0
    ux, uy = vx / n, vy / n
    minx, miny, maxx, maxy = poly.bounds
    far = max([(minx, cy), (maxx, cy), (cx, miny), (cx, maxy)], key=lambda p: (p[0] - dx) * ux + (p[1] - dy) * uy)
    end = (far[0] - ux * 1.2, far[1] - uy * 1.2)
    eye = lvl["elevation_m"] + 1.6
    pts = [(dx - ux * 9, dy - uy * 9), (dx - ux * 4, dy - uy * 4), (dx + ux * 0.6, dy + uy * 0.6), (cx, cy), end]
    return [[round(x, 3), round(y, 3), round(eye, 3)] for x, y in pts]


def _video(frames_dir: Path, fps: int, out: Path) -> dict:
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-framerate", str(fps), "-i", str(frames_dir / "frame_%04d.png"),
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", "-movflags", "+faststart", str(out)],
                       capture_output=True, text=True, timeout=900)
    if r.returncode != 0 or not out.exists():
        raise StudioError(f"ffmpeg failed: {r.stderr.strip()[-300:]}")
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries",
                            "stream=nb_read_frames,width,height:format=duration", "-of", "json", str(out)],
                           capture_output=True, text=True, timeout=300)
    info = json.loads(probe.stdout)
    st = info["streams"][0]
    return {"frames": int(st["nb_read_frames"]), "width": st["width"], "height": st["height"],
            "seconds": round(float(info["format"]["duration"]), 2)}


def render(slug: str, preset: str = "day", views: list[str] | None = None, interiors: int = 2,
           engine: str = "cycles", samples: int = 64, width: int = 1280, height: int = 720,
           enhance: bool = False, video: str | None = None, video_frames: int = 144) -> dict:
    if preset not in PRESETS:
        raise StudioError(f"preset must be one of {PRESETS}")
    views = list(VIEWS) if views is None else views
    if any(v not in VIEWS for v in views):
        raise StudioError(f"views must be from {VIEWS}")
    if video not in (None, "orbit", "path"):
        raise StudioError("video must be orbit or path")
    cfg = config.load()
    exe = cfg["blender"]["executable"]
    if not exe or not Path(exe).is_file():
        raise StudioError("Blender is not configured: set blender.executable in config/studio.yaml")
    p = projects.project_dir(slug)
    model = current_model(slug)
    spec, brief = projects.read_state(slug, "model_spec.json"), projects.read_state(slug, "design_brief.yaml")
    stem = Path(model["path"]).stem
    notes = []
    walk = None
    if video:
        path = walk_path(spec) if video == "path" else None
        if video == "path" and not path:
            notes.append("no front door + hall found in the plan; orbit video made instead")
            video = "orbit"
        walk = {"kind": video, "frames": video_frames, "fps": 24, "width": 960, "height": 540, "samples": 16,
                "path": path or []}
    rooms = pick_interiors(spec, interiors)
    north = (brief.get("site", {}).get("north_degrees") or {}).get("value")
    job = {"stem": stem, "preset": preset, "engine": engine, "samples": samples, "width": width, "height": height,
           "north_degrees": north or 0, "views": views, "interiors": rooms, "lines": True, "walkthrough": walk}
    work = p / "renders" / "_work" / f"{stem}_{preset}"
    work.mkdir(parents=True, exist_ok=True)
    (work / "job.json").write_text(json.dumps(job, indent=1), encoding="utf-8")
    t0 = time.monotonic()
    r = subprocess.run([exe, "--background", "--python", str(SCRIPT), "--", str(p / model["path"]), str(work),
                        str(work / "job.json")], capture_output=True, text=True,
                       timeout=cfg["blender"]["timeout_seconds"])
    report_f = work / f"{stem}_render_report.json"
    if r.returncode != 0 or not report_f.exists():
        tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-12:])
        raise StudioError(f"Blender render failed (exit {r.returncode}):\n{tail}")
    report = json.loads(report_f.read_text(encoding="utf-8"))
    notes += report.get("notes", [])
    rev = model["revision"]
    names = {s["id"]: s for s in rooms}

    def keep(src: str, kind: str, authoritative: bool, note: str) -> str:
        dest = p / "renders" / Path(src).name
        Path(src).replace(dest)
        projects.add_artifact(slug, kind, dest, "visualization_artist", authoritative, note, source_revision=rev)
        return dest.relative_to(p).as_posix()

    stills = []
    for s in report["stills"]:
        item = {"view": s["view"], "seconds": s.get("seconds"),
                "file": keep(s["file"], "render", True, f"{preset}, {report.get('engine')}, model r{rev}")}
        if s.get("lines"):
            item["lines"] = keep(s["lines"], "render_lines", True, "geometry line pass")
        stills.append(item)

    out = {"model": model["path"], "revision": rev, "preset": preset, "engine": report.get("engine"),
           "device": report.get("device"), "stills": stills, "video": None, "enhanced": [], "notes": notes}

    if walk and report.get("walkthrough"):
        (p / "videos").mkdir(exist_ok=True)
        mp4 = p / "videos" / f"{stem}_{preset}_{walk['kind']}.mp4"
        info = _video(Path(report["walkthrough"]["dir"]), walk["fps"], mp4)
        if info["frames"] != walk["frames"]:
            notes.append(f"video has {info['frames']} frames, expected {walk['frames']}")
        projects.add_artifact(slug, "video", mp4, "cinematographer", True,
                              f"{walk['kind']} walkthrough, {info['seconds']} s, model r{rev}", source_revision=rev)
        out["video"] = {"file": mp4.relative_to(p).as_posix(), **info, "kind": walk["kind"]}

    if enhance:
        try:
            out["enhanced"] = _enhance(slug, p, stills, preset, brief.get("preferences") or {}, names, rev, cfg, notes)
        except (StudioError, OSError, ValueError, KeyError) as e:
            # enhancement is optional: the Blender renders and video above are complete and kept
            notes.append(f"enhancement stopped: {e}")
    out["seconds"] = round(time.monotonic() - t0, 1)
    (work / "result.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


def _enhance(slug, p, stills, preset, prefs, rooms, rev, cfg, notes) -> list[dict]:
    c = comfy.Comfy(cfg)
    if not c.up():
        notes.append(f"ComfyUI is not running at {c.base}; enhancement skipped (Blender renders are complete)")
        return []
    threshold = cfg["comfyui"]["enhance"]["min_edge_recall"]
    max_unexplained = cfg["comfyui"]["enhance"]["max_unexplained_share"]
    done, checked = [], False
    try:
        for i, s in enumerate(stills):
            if not s.get("lines"):
                continue
            src, lines = p / s["file"], p / s["lines"]
            room = rooms.get(s["view"].replace("interior_", "")) if s["view"].startswith("interior_") else None
            positive = comfy.prompt_for(prefs, preset, room["type"].replace("_", " ") if room else None)
            t = time.monotonic()
            try:  # one image failing (busy or unreachable ComfyUI) must not lose the others
                graph = comfy.workflow(c.upload(src, src.name), c.upload(lines, lines.name), positive, cfg,
                                       seed=1000 + i, prefix=f"architectural_studio/{src.stem}")
                if not checked:
                    problems = c.check_nodes(graph)
                    if problems:
                        notes.append("ComfyUI cannot run the enhancement workflow: " + "; ".join(problems))
                        return []
                    checked = True
                imgs = c.run(graph)
                if not imgs:
                    notes.append(f"{src.name}: ComfyUI returned no image")
                    continue
                dest = c.fetch(imgs[0], src.with_name(src.stem + "_enhanced.png"))
            except (StudioError, httpx.HTTPError) as e:
                notes.append(f"{src.name}: enhancement failed ({e or type(e).__name__})")
                continue
            check = dest.with_name(dest.stem + "_check.png")
            score = comfy.geometry_score(lines, dest, render_png=src, overlay_png=check)
            status = ("geometry altered" if score["edge_recall"] < threshold else
                      "review red lines" if score["unexplained_share"] > max_unexplained else "geometry kept")
            projects.add_artifact(
                slug, "render_enhanced", dest, "visualization_artist", False,
                f"AI-enhanced from {src.name}: {status} (edge recall {score['edge_recall']}, "
                f"{score['unexplained_segments']} unexplained lines; see {check.name}). Not a design record.",
                source_revision=rev)
            done.append({"view": s["view"], "file": dest.relative_to(p).as_posix(), "source": s["file"],
                         "check": check.relative_to(p).as_posix(), **score, "status": status,
                         "seconds": round(time.monotonic() - t, 1), "prompt": positive})
    finally:
        c.free()  # hand the VRAM back
    return done
