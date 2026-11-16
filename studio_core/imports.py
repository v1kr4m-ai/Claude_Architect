"""Import existing plans and models into the project as reviewable proposals.

  file --(format importer)--> SpecGeometry --(geometry.check)--> imports/I###.json  [proposed]
       --(user approves)--> model_spec.json  [backup + revision + approved decision]

The original file is copied into references/plans/ untouched. Importers live in
studio_core/importers/<format>.py and expose  run(path: Path, **opts) -> (SpecGeometry, notes: list[str]).
"""
import json
import re
import shutil
from pathlib import Path

from . import geometry, projects
from .projects import StudioError

FORMATS = {".dxf": "dxf", ".ifc": "ifc",
           ".png": "plan_image", ".jpg": "plan_image", ".jpeg": "plan_image", ".webp": "plan_image",
           ".pdf": "plan_image",
           ".glb": "mesh", ".gltf": "mesh", ".fbx": "mesh", ".obj": "mesh", ".stl": "mesh",
           ".usd": "mesh", ".usdz": "mesh", ".usdc": "mesh", ".blend": "mesh"}


def _importer(fmt: str):
    from .importers import dxf, ifc, mesh, plan_image  # noqa: F401  (imported lazily: heavy deps)
    return {"dxf": dxf, "ifc": ifc, "plan_image": plan_image, "mesh": mesh}[fmt]


def import_file(slug: str, source: str, author: str = "user", **opts) -> dict:
    src = Path(source).expanduser().resolve()
    if not src.is_file():
        raise StudioError(f"Not a file: {source}")
    fmt = FORMATS.get(src.suffix.lower())
    if not fmt:
        raise StudioError(f"Unsupported plan/model format: {src.suffix}. Supported: {', '.join(sorted(FORMATS))}")
    p = projects.project_dir(slug)
    (p / "references" / "plans").mkdir(parents=True, exist_ok=True)
    idir = p / "imports"
    idir.mkdir(exist_ok=True)
    with projects.lock(slug):
        iid = f"I{len(list(idir.glob('I*.json'))) + 1:03d}"
        kept = p / "references" / "plans" / f"{iid}{src.suffix.lower()}"
        shutil.copy2(src, kept)
    try:
        geo, notes = _importer(fmt).run(kept, **opts)
        geo = geometry.SpecGeometry.model_validate(geo.model_dump())  # re-validate whatever came back
    except StudioError:
        kept.unlink(missing_ok=True)
        raise
    except Exception as e:  # importer bug or unreadable file: keep the project clean
        kept.unlink(missing_ok=True)
        raise StudioError(f"{fmt} import failed: {type(e).__name__}: {e}") from e
    geo.source.update(file=kept.relative_to(p).as_posix(), original_name=src.name, format=fmt)
    checked = geometry.check(geo)
    rec = {"id": iid, "status": "proposed", "created": projects.now(), "author": author,
           "format": fmt, "options": {k: v for k, v in opts.items() if v is not None},
           "notes": notes, **checked, "geometry": geo.model_dump()}
    (idir / f"{iid}.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
    projects.add_artifact(slug, "plan_import", idir / f"{iid}.json", author, authoritative=False)
    return rec


def load(slug: str, iid: str) -> dict:
    if not re.fullmatch(r"I\d{3}", iid or ""):
        raise StudioError(f"Invalid import id: {iid!r}")
    f = projects.project_dir(slug) / "imports" / f"{iid}.json"
    if not f.exists():
        raise StudioError(f"Import not found: {iid}")
    return json.loads(f.read_text(encoding="utf-8"))


def list_imports(slug: str) -> list[dict]:
    d = projects.project_dir(slug) / "imports"
    out = []
    for f in sorted(d.glob("I*.json")) if d.exists() else []:
        r = json.loads(f.read_text(encoding="utf-8"))
        out.append({k: r[k] for k in ("id", "status", "created", "format")} |
                   {"file": r["geometry"]["source"].get("original_name"), "summary": r["summary"],
                    "warnings": len(r["warnings"])})
    return out


def approve(slug: str, iid: str, author: str = "user") -> dict:
    """User action: replace the geometry in model_spec.json with this import."""
    rec = load(slug, iid)
    if rec["status"] != "proposed":
        raise StudioError(f"{iid} is already {rec['status']}")
    g = rec["geometry"]
    decision = projects.add_decision(slug, f"Adopt imported geometry {iid} ({g['source'].get('original_name')})",
                                     json.dumps(rec["summary"]), author, status="approved")

    def apply(spec):
        spec.update(levels=g["levels"], spaces=g["spaces"], walls=g["walls"], openings=g["openings"],
                    geometry_source={"import": iid, **g["source"]})
        spec.pop("doors", None), spec.pop("windows", None)  # superseded by 'openings'

    rev = projects.update_state(slug, "model_spec.json", apply, author, f"approved import {iid}")
    f = projects.project_dir(slug) / "imports" / f"{iid}.json"
    with projects.lock(slug):
        rec.update(status="approved", approved=projects.now(), decision=decision["id"])
        f.write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"import": iid, "decision": decision["id"], "revision": rev, "summary": rec["summary"]}


def reject(slug: str, iid: str, reason: str = "") -> dict:
    rec = load(slug, iid)
    if rec["status"] != "proposed":
        raise StudioError(f"{iid} is already {rec['status']}")
    f = projects.project_dir(slug) / "imports" / f"{iid}.json"
    with projects.lock(slug):
        rec.update(status="rejected", rejected=projects.now(), reject_reason=reason)
        f.write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"import": iid, "status": "rejected"}
