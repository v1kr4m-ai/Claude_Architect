# Implementation Plan

## Principle

One approved `model_spec.json` drives plans, Blender geometry, renders, GLB and video. Language models propose; deterministic code calculates; the user approves. Image and video AI only decorate outputs whose geometry came from the spec.

## Architecture

```
 Claude Code / any MCP client        REST (localhost:8777)        CLI
            \                               |                     /
             +---------------  studio_core (Python)  ------------+
                |            |             |            |
            projects      router        ingest       vision         (Phase 1: built)
            (state,      (capability,  (copy,       (describe ->
             locks,       privacy,      thumbs,      structure,
             revisions)   VRAM)         dedupe)      validate)
                |            |
                |        providers: Ollama (live) | Anthropic (coded) | OpenAI, Gemini (todo)
                |
         planning (P3, shapely) -> geometry spec -> Blender (P4) -> renders (P5) -> ComfyUI (P5)
                                                               \-> GLB          \-> video (P5/6)
```

Agents (Phase 6) are not separate services. Each is a prompt + allowed tools + output schema + write permission, run through the router. Claude Code subagents handle work inside this coding environment; Studio Core's router handles local models, which Claude Code agents cannot run.

## Phases

| Phase | Scope | Status |
|---|---|---|
| 1 Foundation | Project store, config, providers, router, ingest (image/video/PDF), two-step vision analysis, REST, MCP, CLI, tests | **Done** |
| 2 Architectural intelligence | Brief editor + approval API (proposed → approved), room schedule schema, adjacency graph, Design Architect / Planning Architect prompts, promote analysis findings into the brief only via approved decisions | **Done** (v0.3.0): proposals + checks + approval; see CHANGELOG |
| 3 2D planning | Deterministic plan generator on shapely: rectangular room packing from schedule + adjacency, walls with thickness, openings, dimension strings, area table; SVG out, PDF via Blender/Cairo or pdf export; spec validator (overlaps, gaps, min room sizes, door access) | |
| 4 3D geometry | `model_spec.json` → Blender: walls with boolean openings, slabs, roof types, stairs, collections per level, Principled materials, `.blend` revisions, GLB + re-import check; plan-vs-model consistency test | |
| 5 Visualisation | Lighting presets, camera generator, Eevee/Cycles stills, depth/line passes, real ComfyUI API workflows (SDXL + ControlNet depth/lineart, tested), enhanced images flagged non-authoritative; camera paths + FFmpeg assembly | |
| 6 Orchestration | Project Director task graph, specialist agent definitions, task queue with single-writer locks (exists), review gates, execution log | |
| 7 Product | Web UI on the REST API: dashboard, reference gallery, brief editor, plan SVG, GLB viewer (model-viewer), approvals, exports | |

## Phase 2 first deliverables

1. `POST /projects/{slug}/decisions/{id}/approve` and a brief-patch API that only applies approved decisions (with backup).
2. Room schedule + adjacency schema in `design_brief.yaml`, pydantic-validated.
3. `brief.propose()` — reasoning model drafts a brief from requirements text + analysis records; output is a *proposal file*, never written into the brief directly.
4. Tests with fake providers, then one live run with qwen3:8b.

## Hardware rules (enforced in code today)

- One model in VRAM at a time; unload after each step (`keep_alive: 0`).
- Context capped at 8192 tokens for local models.
- Image size capped (`vision.max_image_px`, default 1024).
- Router reports when a model will spill out of VRAM.
- ComfyUI and Ollama must not run heavy jobs simultaneously — Phase 5 will check `/api/ps` and ComfyUI's queue before submitting.
