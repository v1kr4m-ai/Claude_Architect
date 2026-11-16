# Environment Audit

Audited 2026-11-16 on the target machine. Every line below was checked by running a command, not assumed.

## Project directory

The project folder was **empty** at the start. The only prior work was the v0.1 scaffold in `architectural-studio-skill.zip` (see [EXISTING_COMPONENTS.md](EXISTING_COMPONENTS.md)).

## Hardware

| Item | Found |
|---|---|
| GPU | NVIDIA GeForce RTX 5060 Laptop GPU, 8151 MiB VRAM, ~7900 MiB free when idle |
| OS | Windows 11 Home 10.0.26300 |

## Software

| Tool | Status | Detail |
|---|---|---|
| Python | OK | 3.14.3, also 3.10 / 3.9 / 3.13 Store |
| Node.js | OK | v25.8.2 (not used in Phase 1) |
| Ollama | OK | 0.17.1, API on `127.0.0.1:11434`, 18 models (see [MODEL_INVENTORY.md](MODEL_INVENTORY.md)) |
| Blender | OK, **not on PATH** | 5.1.0 Alpha portable build (path set in local `config/studio.yaml`). Headless `--background` works. Bundled Python 3.11.13. Render engines: `BLENDER_EEVEE`, `BLENDER_WORKBENCH`, `CYCLES` (no `BLENDER_EEVEE_NEXT`) |
| Blender MCP | Running | `blender-mcp.exe` process present; separate from this project |
| ComfyUI | Installed, **not running** at audit time | Windows portable build; the install path in the original brief was wrong and is corrected in local config. Port 8188 when started via `run_nvidia_gpu.bat` |
| FFmpeg / ffprobe | OK | 8.1 essentials build on PATH |
| pdftoppm (Poppler) | OK | 25.07.0, used to split PDFs into page images |
| Docker | Installed | 29.9.2; not used (nothing here needs a container) |

### ComfyUI models relevant to later phases (on disk, not yet tested)

- Checkpoints: `sd_xl_base_1.0`, `juggernautXL_ragnarokBy`, `dreamshaper_8`, `sd15`, `flux1-dev-fp8`, SDXL-Turbo
- ControlNet folders: `SDXL/`, `FLUX.1/`, plus `Qwen-Image-InstantX-ControlNet-Inpainting`
- Custom nodes: `comfyui_controlnet_aux` (depth/normal/lineart preprocessors), `comfyui-advanced-controlnet`, `ComfyUI-Hunyuan3DWrapper` (image-to-3D), `comfyui-blender`, `ComfyUI-GGUF`, `ComfyUI-TiledDiffusion`, VideoHelperSuite
- Video: Wan 2.1/2.2, MiniMax H3 (local; ~17 min per 8 s clip at 576×1024 on this GPU, measured in an earlier session)

### Python packages already installed (reused, nothing new needed for Phase 1)

fastapi 0.135, uvicorn 0.42, pydantic 2.12, httpx 0.28, PyYAML 6.0, Pillow 12.2, numpy 2.4, shapely 2.1, opencv-python, mcp 1.27, anthropic 0.89, google-genai, python-multipart. **Not installed:** pytest (tests use stdlib `unittest`), reportlab, PyMuPDF.

## Cloud credentials

`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY`: all **unset**. Cloud providers are coded but cannot be live-tested here. One Ollama model (`gemma4:31b-cloud`) is a cloud proxy to ollama.com and is treated as cloud by the privacy policy.

## Ports

| Port | Owner |
|---|---|
| 8765 | **Taken by another local service** (unrelated, reports version 0.1.0 with ffmpeg tools). The v0.1 scaffold used this port; Studio Core now uses **8777** |
| 8777 | Studio Core REST API (localhost only) |
| 8188 | ComfyUI (when started) |
| 11434 | Ollama |

## Orchestration capabilities in this coding environment

- Claude Code subagents (Agent tool) with per-agent model override (`sonnet`/`opus`/`haiku`), worktree isolation, and multi-agent Workflow scripts.
- These run *Claude* models only. They cannot run Ollama models. Local-model routing therefore lives in Studio Core's router, not in Claude Code agent settings.
- MCP servers attach per session; Studio Core ships its own stdio MCP server.

## Defects found in the v0.1 scaffold

1. `build_parametric.py` crashes on Blender 5.1: hardcodes `BLENDER_EEVEE_NEXT` (`TypeError: enum "BLENDER_EEVEE_NEXT" not found`). Reproduced.
2. Its materials set `diffuse_color` only, which affects the viewport, not renders.
3. `config/studio.example.yaml` points at Blender and ComfyUI paths that do not exist on this machine.
4. API default port 8765 collides with an existing service.
5. ComfyUI workflow JSON is a placeholder (3 nodes, no sampler, no output) — not executable, as its own README says.
6. No tests, no provenance enforcement, no locking, no backups, no revision tracking.
