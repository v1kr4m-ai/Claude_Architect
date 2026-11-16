"""MCP stdio server: the same functions as the REST API, callable by any MCP-capable agent.

Run:  python -m studio_core.mcp_server
Calls the Python layer directly, so the REST service does not need to be running.
"""
from mcp.server.fastmcp import FastMCP

from . import brief, imports, ingest, layout, model3d, projects, router, status, vision

mcp = FastMCP("architectural-studio")


@mcp.tool()
def studio_status() -> dict:
    """GPU, Blender, ComfyUI, and every reachable model provider with real capabilities."""
    return status.report()


@mcp.tool()
def route_task(task: str) -> dict:
    """Which model the studio would use for a task: vision, reasoning, coding, json, light, docs, embedding."""
    c = router.pick(task)
    return {"model": c["model"].to_dict(), "reason": c["reason"], "rejected": c["rejected"]}


@mcp.tool()
def list_models() -> dict:
    """Every task, the models able to do it, the pinned choice and the automatic pick."""
    return router.overview()


@mcp.tool()
def list_projects() -> list[dict]:
    """List all projects."""
    return projects.list_projects()


@mcp.tool()
def create_project(name: str) -> dict:
    """Create a new project with empty brief, model spec and manifests."""
    return projects.create_project(name)


@mcp.tool()
def read_project_state(project: str, file: str) -> dict:
    """Read one state file: project.json, design_brief.yaml, model_spec.json,
    reference_manifest.json, decisions.json, revision_history.json or artifacts.json."""
    return projects.read_state(project, file)


@mcp.tool()
def import_reference(project: str, path: str, note: str = "") -> dict:
    """Copy an image, video or PDF into the project. Videos/PDFs are split into frames/pages."""
    return ingest.import_reference(project, path, note, author="agent")


@mcp.tool()
def analyse_references(project: str, reference: str = "", force: bool = False, model: str = "") -> dict:
    """Run the vision model on one reference id, or on all unanalysed images if empty.
    model: optional model id for this run (see list_models), else the pinned/automatic choice."""
    m = model or None
    return vision.analyse(project, reference, vision_model=m) if reference else vision.analyse_all(project, force, vision_model=m)


@mcp.tool()
def propose_decision(project: str, title: str, detail: str = "", author: str = "agent") -> dict:
    """Record a design decision as 'proposed'. Agents cannot approve; the user does that."""
    return projects.add_decision(project, title, detail, author, "proposed")


@mcp.tool()
def propose_brief(project: str, requirements: str, model: str = "") -> dict:
    """Draft a design brief proposal (rooms, areas, adjacencies, concepts) from requirements text.
    Produces proposals/P###; does not change the brief. Only the user can approve it."""
    rec = brief.propose(project, requirements, reasoning_model=model or None)
    return {"id": rec["id"], "summary": rec["proposal"]["summary"], "warnings": rec["warnings"],
            "areas": rec["areas"], "file": f"proposals/{rec['id']}.md"}


@mcp.tool()
def list_brief_proposals(project: str) -> list[dict]:
    """List brief proposals with status. Approval is a user action (CLI/API/dashboard), not an agent tool."""
    return brief.list_proposals(project)


@mcp.tool()
def import_plan(project: str, path: str, width_m: float = 0, storey_height_m: float = 0,
                unit_scale: float = 0) -> dict:
    """Import an existing plan or 3D model (DXF, IFC, plan image/PDF, GLB/FBX/OBJ/STL/USD/.blend) as a
    reviewable proposal. width_m sets the scale of a plan image. Does not change model_spec.json;
    only the user can approve an import."""
    rec = imports.import_file(project, path, author="agent", width_m=width_m or None,
                              storey_height_m=storey_height_m or None, unit_scale=unit_scale or None)
    return {k: rec[k] for k in ("id", "format", "summary", "warnings", "notes")}


@mcp.tool()
def list_plan_imports(project: str) -> list[dict]:
    """List plan/model imports with status."""
    return imports.list_imports(project)


@mcp.tool()
def generate_plan(project: str) -> dict:
    """Lay out a floor plan from the approved brief. Saved as a proposal (imports/I###) with preview
    drawings and check warnings; only the user can approve it into the model."""
    rec = layout.propose(project, author="agent")
    return {k: rec[k] for k in ("id", "summary", "warnings", "notes", "previews")}


@mcp.tool()
def draw_plans(project: str) -> dict:
    """Draw the approved model geometry as SVG/PDF sheets plus an area schedule CSV in plans/."""
    return layout.draw(project)


@mcp.tool()
def build_model(project: str) -> dict:
    """Build the approved geometry in Blender: .blend, GLB and a preview render, then check the
    model against the plan (walls, openings, heights, footprint, GLB re-import). Takes ~1 minute."""
    return model3d.build(project)


@mcp.tool()
def validate_project(project: str) -> dict:
    """Deterministic checks: files parse, provenance is valid, references and artifacts exist."""
    return projects.validate(project)


if __name__ == "__main__":
    mcp.run(transport="stdio")
