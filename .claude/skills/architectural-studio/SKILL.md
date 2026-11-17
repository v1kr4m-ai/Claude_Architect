---
name: architectural-studio
description: Run the local Architectural Studio engine at D:\Claude\Claude_Architect. Use when the user wants to start an architecture project, import Pinterest images, building photos, drone/camera video, sketches or plan PDFs, analyse references, check project state, or ask about 2D plans, 3D Blender models, GLB, renders or walkthrough video for a building design.
---

# Architectural Studio

Engine lives at `D:\Claude\Claude_Architect`. Run every command from that folder. Read its `README.md` for the full command list and `IMPLEMENTATION_PLAN.md` for what is built.

## What works today (Phases 1-4, plus imports and model choice)

```bash
python -m studio_core status                      # GPU, Blender, ComfyUI, models
python -m studio_core new "<name>"                # create project, prints slug
python -m studio_core import <slug> <files...>    # jpg/png/webp, mp4/mov/mkv, pdf
python -m studio_core analyse <slug>              # local vision, ~1.5-2 min per image
python -m studio_core validate <slug>
python -m studio_core propose <slug> --file requirements.txt   # Phase 2: draft brief, ~1 min
python -m studio_core proposals <slug>
python -m studio_core approve <slug> P001 --concept N          # USER action only
python -m studio_core import-plan <slug> <file> [--width-m W]  # existing DXF/IFC/plan image/PDF/GLB/FBX/OBJ/STL/USD/.blend
python -m studio_core imports <slug>
python -m studio_core approve-import <slug> I001               # USER action only: replaces model geometry
python -m studio_core generate-plan <slug>                     # Phase 3: floor plan proposal + preview drawings
python -m studio_core draw <slug>                              # approved geometry -> plans/plan_L*.svg|pdf + area_schedule.csv
python -m studio_core build-model <slug>                       # Phase 4: .blend + GLB + preview + cutaways + plan-vs-model checks (~10-20 s)
python -m studio_core models                                   # which model does which task
python -m studio_core use <task> <model|auto>                  # USER choice: pin a model
```

For a plan photo/scan, pass `--width-m` with the real overall width if the drawing does not state one; without a scale only room names and written sizes come through. Read `projects/<slug>/imports/I###.json` notes and warnings and tell the user what was and was not extracted before they approve.

Proposal to read and summarise: `projects/<slug>/proposals/P###.md` (rooms, areas, concepts, check warnings, questions). Never run `approve` yourself; show the user the proposal and let them choose.

Output to read and summarise for the user: `projects/<slug>/analysis/reference_analysis.md`.

## What does NOT exist yet

Photoreal renders / ComfyUI enhancement / video (Phase 5). If asked for these, say so plainly and offer the next phase. Never describe a plan, model, render or video as produced unless the file exists and `validate` passes.

## Rules

1. Project files are the memory. Read `design_brief.yaml`, `model_spec.json`, `decisions.json` before advising on a project.
2. Vision analysis is evidence, not a decision. To act on a finding, record it with `propose_decision` (MCP) or `POST /projects/<slug>/decisions`; the user approves.
3. Dimensions carry provenance. Never present a size from a photo as measured.
4. Do not edit state files by hand; use the CLI/API/MCP so locks, backups and revisions apply.
5. Cloud is off (`privacy.allow_cloud: false`). Do not turn it on without the user saying so; their reference photos would leave the machine.
6. ComfyUI is often mid-render for other work: never clear its queue or interrupt it.
7. Outputs are concept material, not construction drawings.
