"""Project store: the files on disk are the project memory, not any chat history.

Every write to a state file goes through write_state(): one writer at a time (lock file),
previous version backed up, atomic replace, revision recorded.
"""
import json
import os
import re
import shutil
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import config

PROVENANCE = ("provided", "measured", "estimated", "unknown")
STATE_FILES = ("project.json", "design_brief.yaml", "model_spec.json", "reference_manifest.json",
               "decisions.json", "revision_history.json", "artifacts.json")
SUBDIRS = ("references/originals", "references/thumbnails", "references/frames", "analysis",
           "plans", "blender", "renders", "videos", "output", "backups", "logs")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class StudioError(Exception):
    """User-facing error (bad input, missing project, lock held)."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(name: str) -> str:
    return "-".join(re.sub(r"[^a-z0-9]+", " ", name.lower()).split())[:64]


def root() -> Path:
    return Path(config.load()["paths"]["projects_root"]).expanduser().resolve()


def project_dir(slug: str) -> Path:
    # Slug regex is the path-traversal guard: no separators, dots or drive letters get through.
    if not _SLUG.match(slug or ""):
        raise StudioError(f"Invalid project id: {slug!r}")
    p = root() / slug
    if not (p / "project.json").exists():
        raise StudioError(f"Project not found: {slug}")
    return p


def _dump(name: str, data) -> str:
    if name.endswith(".yaml"):
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    return json.dumps(data, indent=2, ensure_ascii=False)


def _parse(name: str, text: str):
    return yaml.safe_load(text) if name.endswith(".yaml") else json.loads(text)


def dim(value=None, provenance="unknown", note=""):
    """A dimension always carries where it came from."""
    return {"value": value, "provenance": provenance, "note": note}


def _templates(name: str, slug: str) -> dict:
    return {
        "project.json": {"name": name, "slug": slug, "units": "metres", "location": None,
                         "status": "draft", "revision": 1, "created": now(), "updated": now()},
        "design_brief.yaml": {
            "requirements": {"building_type": None, "floors": None, "bedrooms": None,
                             "bathrooms": None, "room_schedule": [], "functional": []},
            "site": {"plot_width_m": dim(), "plot_length_m": dim(), "north_degrees": dim(),
                     "setbacks_m": {}, "slope": "unknown", "climate": None},
            "preferences": {"style": None, "roof": None, "materials": [], "budget": None},
            "approved_decisions": [], "open_questions": [], "assumptions": []},
        "model_spec.json": {
            "units": "metres",
            "coordinate_system": {"origin": "plot south-west corner", "x": "east", "y": "north", "z": "up"},
            "levels": [], "grids": [], "spaces": [], "walls": [], "slabs": [], "roofs": [],
            "doors": [], "windows": [], "stairs": [], "terrain": None, "materials": [],
            "approved": False},
        "reference_manifest.json": {"references": []},
        "decisions.json": {"decisions": []},
        "revision_history.json": {"revisions": [{"revision": 1, "at": now(), "author": "studio",
                                                 "file": "*", "note": "project created"}]},
        "artifacts.json": {"artifacts": []},
    }


def create_project(name: str) -> dict:
    slug = slugify(name)
    if not _SLUG.match(slug):
        raise StudioError("Project name must contain letters or numbers")
    p = root() / slug
    if p.exists():
        raise StudioError(f"Project already exists: {slug}")
    for sub in SUBDIRS:
        (p / sub).mkdir(parents=True)
    for fname, data in _templates(name, slug).items():
        (p / fname).write_text(_dump(fname, data), encoding="utf-8")
    return read_state(slug, "project.json")


def list_projects() -> list[dict]:
    r = root()
    if not r.exists():
        return []
    out = []
    for p in sorted(r.iterdir()):
        try:
            out.append(json.loads((p / "project.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def read_state(slug: str, name: str):
    if name not in STATE_FILES:
        raise StudioError(f"Unknown state file: {name}")
    return _parse(name, (project_dir(slug) / name).read_text(encoding="utf-8"))


@contextmanager
def lock(slug: str, wait_s: float = 10.0):
    """Single-writer lock. Stale locks (>5 min, e.g. a crashed process) are taken over."""
    path = project_dir(slug) / ".lock"
    deadline = time.monotonic() + wait_s
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {now()}".encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > 300:
                    path.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise StudioError(f"Project {slug} is locked by another writer")
            time.sleep(0.1)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def _write(p: Path, name: str, data) -> None:
    tmp = p / (name + ".tmp")
    tmp.write_text(_dump(name, data), encoding="utf-8")
    os.replace(tmp, p / name)


def write_state(slug: str, name: str, data, author: str = "studio", note: str = "") -> int:
    """Replace a state file. Returns the new project revision."""
    if name not in STATE_FILES or name in ("project.json", "revision_history.json"):
        raise StudioError(f"{name} cannot be written directly")
    _parse(name, _dump(name, data))  # must round-trip before anything on disk is touched
    with lock(slug):
        return _commit(slug, name, data, author, note)


def update_state(slug: str, name: str, fn, author: str = "studio", note: str = "") -> int:
    """Read-modify-write under one lock, so concurrent appenders cannot lose each other's rows."""
    with lock(slug):
        data = read_state(slug, name)
        fn(data)
        return _commit(slug, name, data, author, note)


def _commit(slug: str, name: str, data, author: str, note: str) -> int:
    p = project_dir(slug)
    meta = read_state(slug, "project.json")
    rev = meta["revision"] + 1
    shutil.copy2(p / name, p / "backups" / f"r{meta['revision']:04d}_{name}")
    _write(p, name, data)
    hist = read_state(slug, "revision_history.json")
    hist["revisions"].append({"revision": rev, "at": now(), "author": author, "file": name, "note": note})
    _write(p, "revision_history.json", hist)
    meta.update(revision=rev, updated=now())
    _write(p, "project.json", meta)
    return rev


def add_decision(slug: str, title: str, detail: str, author: str, status: str = "proposed") -> dict:
    """Decisions start as 'proposed'; only an explicit approval call makes them 'approved'."""
    if status not in ("proposed", "approved", "rejected"):
        raise StudioError(f"Bad decision status: {status}")
    entry = {}

    def fn(d):
        entry.update(id=f"D{len(d['decisions']) + 1:03d}", title=title, detail=detail,
                     status=status, author=author, at=now())
        d["decisions"].append(entry)

    update_state(slug, "decisions.json", fn, author, f"decision: {title}")
    return entry


def set_decision_status(slug: str, did: str, status: str, author: str = "user") -> dict:
    """User action only: never exposed to agents through MCP."""
    if status not in ("approved", "rejected"):
        raise StudioError(f"Bad decision status: {status}")
    found = {}

    def fn(d):
        e = next((x for x in d["decisions"] if x["id"] == did), None)
        if e is None:
            raise StudioError(f"Decision not found: {did}")
        e.update(status=status, decided_by=author, decided=now())
        found.update(e)

    update_state(slug, "decisions.json", fn, author, f"decision {did} {status}")
    return found


def add_artifact(slug: str, kind: str, path: Path, author: str, authoritative: bool, note: str = "") -> dict:
    p = project_dir(slug)
    if not path.exists() or path.stat().st_size == 0:
        raise StudioError(f"Artifact missing or empty: {path}")
    entry = {}

    def fn(d):
        entry.update(kind=kind, path=path.relative_to(p).as_posix(), bytes=path.stat().st_size,
                     authoritative=authoritative, author=author, note=note, at=now(),
                     revision=read_state(slug, "project.json")["revision"] + 1)
        d["artifacts"] = [a for a in d["artifacts"] if a["path"] != entry["path"]] + [entry]

    update_state(slug, "artifacts.json", fn, author, f"artifact: {kind}")
    return entry


def _check_dims(node, where, problems):
    if isinstance(node, dict):
        if "provenance" in node and "value" in node:
            if node["provenance"] not in PROVENANCE:
                problems.append(f"{where}: bad provenance {node['provenance']!r}")
            if node["value"] is None and node["provenance"] != "unknown":
                problems.append(f"{where}: no value but provenance is {node['provenance']!r}")
            if node["value"] is not None and node["provenance"] == "unknown":
                problems.append(f"{where}: has a value but provenance is 'unknown'")
        for k, v in node.items():
            _check_dims(v, f"{where}.{k}", problems)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _check_dims(v, f"{where}[{i}]", problems)


def validate(slug: str) -> dict:
    """Deterministic checks. Reports problems; never fixes anything."""
    p = project_dir(slug)
    problems, state = [], {}
    for name in STATE_FILES:
        try:
            state[name] = _parse(name, (p / name).read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError) as e:
            problems.append(f"{name}: unreadable ({e})")
    for sub in SUBDIRS:
        if not (p / sub).is_dir():
            problems.append(f"missing folder {sub}")
    for name in ("design_brief.yaml", "model_spec.json"):
        _check_dims(state.get(name), name, problems)
    if state.get("model_spec.json", {}).get("units") != state.get("project.json", {}).get("units"):
        problems.append("units differ between project.json and model_spec.json")
    for ref in state.get("reference_manifest.json", {}).get("references", []):
        if not (p / ref["original"]).exists():
            problems.append(f"reference {ref['id']}: original file missing")
    for art in state.get("artifacts.json", {}).get("artifacts", []):
        f = p / art["path"]
        if not f.exists() or f.stat().st_size == 0:
            problems.append(f"artifact missing or empty: {art['path']}")
    return {"valid": not problems, "problems": problems,
            "revision": state.get("project.json", {}).get("revision")}
