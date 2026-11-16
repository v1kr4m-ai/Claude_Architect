# Model Inventory

Capabilities come from Ollama's own `/api/show` report, not from model names. Live list: `python -m studio_core status`.

## Ollama (local unless noted)

| Model | Size | Capabilities (reported) | Studio use |
|---|---|---|---|
| llama3.2-vision:latest | 7.8 GB, 10.7B Q4_K_M | completion, vision | **Vision step 1** (default). Slightly over the 8 GB budget, spills a little to RAM; ~70 s per image |
| llava:latest | 4.7 GB, 7B Q4_0 | completion, vision | Vision fallback. Fits GPU, ~11 s, but less accurate (see below) |
| moondream:latest | 1.7 GB, 1B | completion, vision | **Excluded**: degenerate output on the analysis prompt |
| qwen2.5:7b | 4.7 GB, 7.6B Q4_K_M | completion, tools | **JSON structuring** (vision step 2), docs |
| qwen3:8b | 5.2 GB, 8.2B Q4_K_M | completion, tools, thinking | Reasoning (local) |
| deepseek-r1:8b | 5.2 GB | completion, thinking | Reasoning fallback |
| qwen3-coder:30b | 18 GB, MoE Q4_K_M | completion, tools | Coding (local, spills to RAM) |
| deepseek-coder:6.7b | 3.8 GB | completion | Coding fallback |
| gpt-oss:20b / latest | 13 GB | completion, tools, thinking | Unused; available |
| glm-4.7-flash | 19 GB | completion, tools | Unused; available |
| phi4 | 9.1 GB | completion | Unused |
| llama3.2:latest | 2.0 GB | completion, tools | Light classification |
| nomic-embed-text | 274 MB | embedding | Embeddings (Phase 2 reference search) |
| gemma4:31b-cloud | proxy | completion, vision, tools, thinking | **Cloud** (ollama.com). Blocked unless `privacy.allow_cloud: true` |
| other local models | | | Unrelated projects; never routed |

## Cloud (coded, not live-tested: no credentials on this machine)

| Provider/model | Use |
|---|---|
| anthropic/claude-opus-5-5 | First choice for reasoning and coding when cloud is allowed |
| anthropic/claude-haiku-5-5 | Available for cheap bulk work |

OpenAI and Gemini adapters are **not written yet**. The provider interface is four methods (`available`, `models`, `chat`, `name`); see `studio_core/providers.py`.

## Vision benchmark (2026-11-16)

Same Blender test render (2-storey flat-roof box, 12 × 8 m, 4 windows per floor per visible face, one door).

**One-step (vision model fills full JSON schema directly):**

| Model | Result |
|---|---|
| llava | Said walls are brick, storeys unknown, openings "not visible" |
| llama3.2-vision | "rectangular, flat, concrete"; openings and features "not visible" |
| moondream | Garbage: numbers as text, repeated tokens |

**Two-step (vision model describes freely, qwen2.5:7b fills schema):**

| Vision model | Result |
|---|---|
| llama3.2-vision | Correct: render, 2 storeys, flat roof, modern, central entrance, windows per face roughly right; size estimate ~10 m (true 12 m), correctly marked *estimated* |
| llava | Wrong storey count (said single-storey) |

Conclusion, built into the code: **two-step pipeline, llama3.2-vision then qwen2.5:7b**. Models run one at a time and are unloaded after use (`keep_alive: 0`), so the 8 GB card never holds two. `num_ctx` is capped at 8192 because Ollama's default 32k context pushed llava off the GPU.

Known ceiling: local vision is good for style/massing/roof/storeys, weak on materials and exact counts. For serious reference analysis, enabling a cloud vision model is the upgrade path.

## Routing defaults

Set in `studio_core/config.py` `DEFAULTS["routing"]`, overridable in `config/studio.yaml`. Listed preferences win; among unlisted models, fits-in-VRAM first, then largest.
