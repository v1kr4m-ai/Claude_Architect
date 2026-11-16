"""REST API. Thin layer over the Python functions; binds to localhost only (see __main__)."""
from fastapi import FastAPI, HTTPException
from pathlib import Path

from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import brief, ingest, projects, providers, router, status, vision

app = FastAPI(title="Architectural Studio Core", version="0.3.0")


@app.exception_handler(projects.StudioError)
async def _studio_error(_, e):
    code = 404 if "not found" in str(e).lower() else 409 if "exists" in str(e) or "locked" in str(e) else 400
    return JSONResponse({"detail": str(e)}, status_code=code)


@app.exception_handler(providers.ProviderError)
async def _provider_error(_, e):
    return JSONResponse({"detail": str(e)}, status_code=503)


class NewProject(BaseModel):
    name: str


class NewReference(BaseModel):
    path: str  # local file path; this API is localhost-only and single-user
    note: str = ""


class Analyse(BaseModel):
    reference: str | None = None  # None = every unanalysed image
    force: bool = False


class Decision(BaseModel):
    title: str
    detail: str = ""
    author: str = "user"
    status: str = "proposed"


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(Path(__file__).parent / "ui" / "index.html", media_type="text/html")


@app.get("/projects/{slug}/files/{path:path}")
def project_file(slug: str, path: str):
    """Read-only; only thumbnails, originals and analysis JSON. Never state files or backups."""
    pdir = projects.project_dir(slug).resolve()
    target = (pdir / path).resolve()
    allowed = (pdir / "references" / "thumbnails", pdir / "analysis", pdir / "references" / "originals")
    if not (any(target.is_relative_to(a) for a in allowed) and target.is_file()):
        raise HTTPException(404, "File not found")
    return FileResponse(target)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/status")
def get_status():
    return status.report()


@app.get("/route/{task}")
def route(task: str):
    c = router.pick(task)
    return {"model": c["model"].to_dict(), "reason": c["reason"], "rejected": c["rejected"]}


@app.get("/projects")
def list_projects():
    return {"projects": projects.list_projects()}


@app.post("/projects", status_code=201)
def create_project(req: NewProject):
    return projects.create_project(req.name)


@app.get("/projects/{slug}/state/{name}")
def get_state(slug: str, name: str):
    return projects.read_state(slug, name)


@app.post("/projects/{slug}/references")
def add_reference(slug: str, req: NewReference):
    return ingest.import_reference(slug, req.path, req.note)


@app.post("/projects/{slug}/analyse")
def analyse(slug: str, req: Analyse):
    # sync def: FastAPI runs it in a worker thread, so a slow local model does not block /health
    if req.reference:
        return vision.analyse(slug, req.reference)
    return vision.analyse_all(slug, req.force)


@app.post("/projects/{slug}/decisions", status_code=201)
def add_decision(slug: str, req: Decision):
    return projects.add_decision(slug, req.title, req.detail, req.author, req.status)


class Requirements(BaseModel):
    text: str


class Approve(BaseModel):
    concept: int = 0


class Reject(BaseModel):
    reason: str = ""


class DecisionStatus(BaseModel):
    status: str


@app.post("/projects/{slug}/proposals", status_code=201)
def propose(slug: str, req: Requirements):
    return brief.propose(slug, req.text)


@app.get("/projects/{slug}/proposals")
def proposals(slug: str):
    return {"proposals": brief.list_proposals(slug)}


@app.get("/projects/{slug}/proposals/{pid}")
def proposal(slug: str, pid: str):
    return brief.load(slug, pid)


@app.post("/projects/{slug}/proposals/{pid}/approve")
def approve(slug: str, pid: str, req: Approve):
    return brief.approve(slug, pid, req.concept)


@app.post("/projects/{slug}/proposals/{pid}/reject")
def reject(slug: str, pid: str, req: Reject):
    return brief.reject(slug, pid, req.reason)


@app.post("/projects/{slug}/decisions/{did}")
def decide(slug: str, did: str, req: DecisionStatus):
    return projects.set_decision_status(slug, did, req.status)


@app.post("/projects/{slug}/validate")
def validate(slug: str):
    return projects.validate(slug)
