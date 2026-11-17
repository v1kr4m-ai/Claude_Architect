"""REST API. Thin layer over the Python functions; binds to localhost only (see __main__)."""
from fastapi import FastAPI, HTTPException
from pathlib import Path

from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import brief, imports, ingest, layout, model3d, projects, providers, router, status, vision, visualize

app = FastAPI(title="Architectural Studio Core", version="0.6.0")


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
    model: str | None = None      # vision model for this run


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
    allowed = (pdir / "references" / "thumbnails", pdir / "analysis", pdir / "references" / "originals",
               pdir / "plans", pdir / "renders", pdir / "output", pdir / "videos")  # drawings, renders, GLB deliverables
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


class ModelChoice(BaseModel):
    model: str | None = None  # None = automatic


@app.get("/models")
def models():
    return router.overview()


@app.put("/models/{task}")
def choose_model(task: str, req: ModelChoice):
    return router.set_choice(task, req.model)


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
        return vision.analyse(slug, req.reference, vision_model=req.model)
    return vision.analyse_all(slug, req.force, vision_model=req.model)


@app.post("/projects/{slug}/decisions", status_code=201)
def add_decision(slug: str, req: Decision):
    return projects.add_decision(slug, req.title, req.detail, req.author, req.status)


class Requirements(BaseModel):
    text: str
    model: str | None = None  # reasoning model for this run


class Approve(BaseModel):
    concept: int = 0


class Reject(BaseModel):
    reason: str = ""


class DecisionStatus(BaseModel):
    status: str


@app.post("/projects/{slug}/proposals", status_code=201)
def propose(slug: str, req: Requirements):
    return brief.propose(slug, req.text, reasoning_model=req.model)


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


class PlanImport(BaseModel):
    path: str                       # local file; API is localhost-only, single user
    width_m: float | None = None
    storey_height_m: float | None = None
    unit_scale: float | None = None
    model: str | None = None


@app.post("/projects/{slug}/imports", status_code=201)
def import_plan(slug: str, req: PlanImport):
    return imports.import_file(slug, req.path, width_m=req.width_m, storey_height_m=req.storey_height_m,
                               unit_scale=req.unit_scale, vision_model=req.model)


@app.get("/projects/{slug}/imports")
def list_imports(slug: str):
    return {"imports": imports.list_imports(slug)}


@app.get("/projects/{slug}/imports/{iid}")
def get_import(slug: str, iid: str):
    return imports.load(slug, iid)


@app.post("/projects/{slug}/imports/{iid}/approve")
def approve_import(slug: str, iid: str):
    return imports.approve(slug, iid)


@app.post("/projects/{slug}/imports/{iid}/reject")
def reject_import(slug: str, iid: str, req: Reject):
    return imports.reject(slug, iid, req.reason)


@app.post("/projects/{slug}/plans/generate", status_code=201)
def generate_plan(slug: str):
    return layout.propose(slug)


@app.post("/projects/{slug}/plans/draw")
def draw_plan(slug: str):
    return layout.draw(slug)


@app.post("/projects/{slug}/model/build")
def build_model(slug: str):
    return model3d.build(slug)


class RenderJob(BaseModel):
    preset: str = "day"
    views: list[str] | None = None
    interiors: int = 2
    engine: str = "cycles"
    samples: int = 64
    width: int = 1280
    height: int = 720
    enhance: bool = False
    video: str | None = None
    video_frames: int = 144


@app.post("/projects/{slug}/render")
def render(slug: str, req: RenderJob):
    return visualize.render(slug, **req.model_dump())


@app.post("/projects/{slug}/validate")
def validate(slug: str):
    return projects.validate(slug)
