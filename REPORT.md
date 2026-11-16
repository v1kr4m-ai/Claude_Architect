# Phase 1 Report — 2026-11-16

## Verdict

Phase 1 exit criterion met: **a user can create a project, import reference images (and video, and PDF), analyse them with a local vision model, and get validated structured results.** Demonstrated end to end on this machine with real models, not mocks.

## Demonstrated live

| Step | Evidence |
|---|---|
| Create project | `projects/demo-massing-house/` with 7 state files, 11 folders |
| Import image | REF001: original byte-identical, thumbnail, SHA-256, dHash |
| Import video | REF002 (6 s mp4) → 3 frames via ffmpeg |
| Import PDF | REF003 (2 pages) → 2 page images via pdftoppm; page 1 auto-flagged near-duplicate of REF001 |
| Exact duplicate | Re-import skipped (test) |
| Vision analysis | llama3.2-vision described, qwen2.5:7b structured, 0 validation retries, ~100 s. Correct: render, 2 storeys, flat roof, modern. Size estimate 10 m vs true 12 m, labelled *estimated* |
| VRAM | One model at a time; VRAM back to 0 MiB after the run |
| Validation | `validate` → valid, revision tracked, backups written |
| REST | `/health`, `/projects`, `/route/vision`, `/validate` answered on 127.0.0.1:8777 |
| MCP | 9 tools listed; `validate_project` called in-process |
| Blender | 5.1 headless: `.blend` + PNG render + GLB written (massing smoke test) |
| Tests | 15/15 pass (`python -m unittest discover tests`) |

## What does not work / was not tested

- **Cloud providers**: Anthropic adapter written but never called (no API key). OpenAI and Gemini adapters not written.
- **Local vision quality**: good on type/storeys/roof/style, weak on materials and exact window counts. Moondream is unusable for this and is excluded. Test image was a simple synthetic render; real Pinterest photos not tested yet.
- **Pinterest URLs**: not supported. Users save the image and import the file. (Fetching Pinterest pages runs into their terms and login wall.)
- **ComfyUI**: installed but was not running; no workflow built or tested. Phase 5.
- **Plans, spec-driven 3D, renders, video**: not built (Phases 3–5). The Blender script is a smoke test from plain numbers, not from `model_spec.json`.
- **Agents**: no Project Director or specialist agents yet (Phase 6). The router + lock + revision log they will run on are built.
- **Port 8765** is owned by another service on this machine; Studio uses 8777.

## Decisions made without asking

| Decision | Why |
|---|---|
| Built in a dedicated project folder, not inside the skill zip | Empty project folder named for this; zip kept untouched |
| Rewrote rather than extended the scaffold | It crashed on Blender 5.1 and had no state model; ~10% of its code survived (see EXISTING_COMPONENTS.md) |
| Two-step vision (describe, then structure) | Measured: one-step output from both local vision models was near-useless |
| Python stdlib `unittest`, no pytest | pytest not installed; no new dependency needed |
| No Docker | Nothing here needs a container |
| `projects/` git-ignored | Holds private reference photos |
| Nothing committed to git | Not asked |

## Next milestone: Phase 2 (architectural intelligence)

Exit: user gives requirements text + references and receives a structured, reviewable brief proposal.

1. Decision approval endpoint; brief changes only through approved decisions.
2. Room schedule + adjacency schema with validation.
3. Design/Planning Architect prompts producing proposal files (qwen3:8b locally; Claude when cloud is enabled).
4. Tests with fakes, one live run.

Phase 3 (deterministic 2D plans on shapely → SVG) is the first visible deliverable after that, and the one most worth getting right: everything 3D derives from it.

## Exact commands

See [README.md](README.md). The skill is installed at `%USERPROFILE%\.claude\skills\architectural-studio\SKILL.md` (copy of [SKILL.md](SKILL.md)); new Claude Code sessions will see it.
