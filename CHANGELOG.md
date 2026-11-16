# Changelog

## 0.4.0 — 2026-11-16 — Import existing work; model selection

- `geometry.py`: authoritative geometry schema for `model_spec.json` (levels, spaces with polygons, walls with thickness, openings; provenance per element) and deterministic checks (overlaps, tiny rooms, unknown thicknesses, implausible units).
- `imports.py` + `importers/`: existing plans and models become reviewable `imports/I###.json`; user approval replaces the model geometry with backup, revision and decision.
  - DXF (ezdxf): units from `$INSUNITS`, double-line walls merged with real thickness, rooms from closed outlines + text labels, doors/windows from blocks. Live: 12 x 9 m test house → 6 walls at 0.23 m, 3 named rooms, 2 doors.
  - IFC (ifcopenshell): storeys, walls, spaces, doors, windows from real geometry.
  - Plan image/PDF (OpenCV + two-step vision): axis-aligned walls scaled from one width; room names and written sizes, with feet-inches parsed in code.
  - 3D models via headless Blender: GLB/GLTF/FBX/OBJ/STL/USD/.blend → footprint, storeys from slabs, preview render; terrain/ground planes detected and excluded (live: 28 x 24 m with ground → 13 x 9 m building envelope).
- Model selection: per-run `--model`, pinned per-task choices (`use`, `PUT /models/{task}`, dashboard picker), `models` overview. Invalid choices refused, never silently replaced.
- Dashboard: model picker, brief proposals and plan imports with approve/reject.
- Fixes: `config.load()` leaked mutations into defaults (now deep-copied); `/status` 16 s → 3 s and `/models` 13 s → 0.3 s (60 s model-list cache, Blender version cached).
- Dependencies added: ezdxf, ifcopenshell (user-approved).
- 44 tests. Importers written by Sonnet subagents from a spec, reviewed and live-tested.

## 0.3.0 — 2026-11-16 — Phase 2: design brief proposals

- `brief.py`: requirements text + reference analyses -> local reasoning model (qwen3:8b, thinking off) drafts building type, floors, rooms with areas and levels, adjacencies, 1-3 concepts, assumptions, questions.
- Deterministic Planning Architect checks: provenance verified against the requirements text (an area counts as stated only with an area unit; a length only with a length unit or as a side of "12 x 20"), duplicate rooms, unknown adjacencies, rooms above the top floor, missing stair per level, rule-of-thumb minimum areas, rooms with no connection, gross vs target, ground floor vs plot, unknown reference citations.
- Room type synonyms (parking -> garage, toilet -> wc, ...).
- Proposals saved as `proposals/P###.json` + readable `.md`. Approve (user only) writes the chosen concept into `design_brief.yaml` with backup, revision and an approved decision. Reject, list, and decision approve/reject.
- CLI: `propose`, `proposals`, `approve`, `reject`, `decide`. REST: `/projects/{slug}/proposals...`, `/decisions/{id}`. MCP: `propose_brief`, `list_brief_proposals` (agents cannot approve).
- Providers: per-call `max_tokens`, `think` switch; router now uses each model's real loaded size once seen (cached in `.cache/`).
- CLI forces UTF-8 output (Windows cp1252 crashed on non-ASCII).
- `Start_Studio.bat` launcher; server exits with its window and frees the port.
- Public-repo hygiene: `config/studio.example.yaml` shipped, `config/studio.yaml` git-ignored, local paths scrubbed from docs.
- Live run: 14-room proposal for a 2-storey house in 57 s; checks flagged missing stairs and a 16% area overrun.
- 26 tests.

## 0.2.1 — 2026-11-16

- Read-only dashboard at `/` (built by a Sonnet subagent, reviewed and browser-tested); file endpoint serves only thumbnails, originals and analysis JSON.
- Fix: provider timeouts no longer crash a whole batch; each failure is recorded and the batch continues.
- Fix: llama3.2-vision could loop forever on some images (>600 s); local output now capped with `num_predict: 1200` (same frame: 106 s).
- Known: router judges VRAM fit by file size; llama3.2-vision loads at 12 GB, not 7.4 GB (ponytail note in router.py).
- 17 tests.

## 0.2.0 — 2026-11-16 — Phase 1 foundation

- Project store with 7 state files, single-writer lock, backups, revision history.
- Dimension provenance (`provided | measured | estimated | unknown`) validated everywhere.
- Config: defaults ← `config/studio.yaml` ← env vars. Verified machine paths for Blender 5.1 and ComfyUI.
- Providers: Ollama (live, capabilities from `/api/show`), Anthropic (coded, untested: no key).
- Router: capability + privacy + VRAM-aware; Ollama `*-cloud` models treated as cloud.
- Ingest: images, videos (ffmpeg frames), PDFs (pdftoppm pages); SHA-256 exact dedupe, dHash near-duplicate flag, thumbnails, originals untouched.
- Vision Analyst: two-step (llama3.2-vision describes, qwen2.5:7b structures), pydantic validation with retry, photo dimensions forced to `estimated`/`unknown`, one model in VRAM at a time, `num_ctx` 8192.
- REST API (localhost:8777, refuses non-localhost bind), MCP stdio server, CLI.
- 15 unit tests.
- Blender massing smoke test ported from v0.1 with the Blender 5.x engine bug fixed.
- API port moved 8765 → 8777 (8765 is used by another local service).
