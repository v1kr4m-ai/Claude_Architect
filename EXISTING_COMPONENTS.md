# Existing Components

What existed before this build, and what happened to each piece.

## v0.1 scaffold (`architectural-studio-skill.zip`, 2026-11-15)

| Scaffold file | Status | Now |
|---|---|---|
| `SKILL.md` | Good principles, kept | Rewritten as [SKILL.md](SKILL.md) around the real commands |
| `studio_core/api.py` | Ran, but no locking, backups, provenance, ingest or vision | Replaced by `studio_core/api.py` + `projects.py` |
| `studio_core/config.py` | Worked | Replaced: adds defaults, env overrides, privacy, routing |
| `studio_core/mcp_server.py` | Proxied to REST (needed server running) | Replaced: calls Python directly |
| `engines/blender/build_parametric.py` | **Crashed** on Blender 5.1 (`BLENDER_EEVEE_NEXT`) | Ported and fixed as `engines/blender/massing_preview.py` (smoke test only) |
| `engines/blender/export_glb.py` | Untested in scaffold | Folded into `massing_preview.py`; GLB export verified |
| `scripts/extract_video_frames.py` | Worked conceptually (one ffmpeg call per frame) | Replaced by `ingest.py` (one ffmpeg call, fixed interval) |
| `scripts/new_project.py`, `validate_project.py`, `run_blender.py` | Worked | Replaced by `python -m studio_core new / validate` |
| `templates/project_template/` | Old single-box schema (`building.width_m`) | Replaced by the 7-file project state with provenance |
| `workflows/comfyui/exterior_api_template.json` | Placeholder, not executable | Not carried over; Phase 5 builds real workflows against the installed nodes |
| `config/studio.example.yaml` | Wrong paths for this machine | Replaced by `config/studio.yaml` with verified paths |

The original zip is untouched in `Downloads`.

## Machine assets reused (not reinstalled)

Ollama + its 18 models, Blender 5.1 portable, ComfyUI portable + its models and custom nodes, FFmpeg, Poppler, and the Python packages listed in [ENVIRONMENT_AUDIT.md](ENVIRONMENT_AUDIT.md). No new dependency was installed for Phase 1.
