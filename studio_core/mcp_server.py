"""MCP stdio server: the same functions as the REST API, callable by any MCP-capable agent.

Run:  python -m studio_core.mcp_server
Calls the Python layer directly, so the REST service does not need to be running.
"""
from mcp.server.fastmcp import FastMCP

from . import brief, ingest, projects, router, status, vision

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
def analyse_references(project: str, reference: str = "", force: bool = False) -> dict:
    """Run the local vision model on one reference id, or on all unanalysed images if empty."""
    return vision.analyse(project, reference) if reference else vision.analyse_all(project, force)


@mcp.tool()
def propose_decision(project: str, title: str, detail: str = "", author: str = "agent") -> dict:
    """Record a design decision as 'proposed'. Agents cannot approve; the user does that."""
    return projects.add_decision(project, title, detail, author, "proposed")


@mcp.tool()
def propose_brief(project: str, requirements: str) -> dict:
    """Draft a design brief proposal (rooms, areas, adjacencies, concepts) from requirements text.
    Produces proposals/P###; does not change the brief. Only the user can approve it."""
    rec = brief.propose(project, requirements)
    return {"id": rec["id"], "summary": rec["proposal"]["summary"], "warnings": rec["warnings"],
            "areas": rec["areas"], "file": f"proposals/{rec['id']}.md"}


@mcp.tool()
def list_brief_proposals(project: str) -> list[dict]:
    """List brief proposals with status. Approval is a user action (CLI/API/dashboard), not an agent tool."""
    return brief.list_proposals(project)


@mcp.tool()
def validate_project(project: str) -> dict:
    """Deterministic checks: files parse, provenance is valid, references and artifacts exist."""
    return projects.validate(project)


if __name__ == "__main__":
    mcp.run(transport="stdio")
