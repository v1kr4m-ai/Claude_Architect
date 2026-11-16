"""Configuration: built-in defaults <- config/studio.yaml <- environment variables."""
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

DEFAULTS = {
    "api": {"host": "127.0.0.1", "port": 8777},
    "paths": {"projects_root": str(ROOT / "projects")},
    "blender": {"executable": "", "timeout_seconds": 1800},
    "comfyui": {"base_url": "http://127.0.0.1:8188", "timeout_seconds": 900},
    "ollama": {"base_url": "http://127.0.0.1:11434", "timeout_seconds": 600, "num_ctx": 8192, "num_predict": 1200},
    # Cloud use is opt-in. Ollama "*-cloud" models count as cloud: they proxy to ollama.com.
    "privacy": {"allow_cloud": False},
    "hardware": {"vram_headroom_mb": 600},
    # Ordered preference per task; first available model that passes privacy + capability wins.
    "routing": {
        "vision": ["ollama/llama3.2-vision:latest", "ollama/llava:latest"],
        "reasoning": ["anthropic/claude-opus-5-5", "ollama/qwen3:8b", "ollama/deepseek-r1:8b"],
        "coding": ["anthropic/claude-opus-5-5", "ollama/qwen3-coder:30b", "ollama/deepseek-coder:6.7b"],
        "json": ["ollama/qwen2.5:7b", "ollama/qwen3:8b"],
        "light": ["ollama/llama3.2:latest", "ollama/qwen2.5:7b"],
        "docs": ["ollama/qwen2.5:7b", "ollama/qwen3:8b"],
        "embedding": ["ollama/nomic-embed-text:latest"],
    },
    "vision": {"max_image_px": 1024, "retries": 1},
    "ingest": {"thumbnail_px": 384, "video_frame_interval_s": 2.0, "video_max_frames": 24,
               "near_duplicate_hamming": 5},
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load() -> dict:
    path = Path(os.getenv("STUDIO_CONFIG", ROOT / "config" / "studio.yaml"))
    cfg = _merge(DEFAULTS, yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {})
    for env, (section, key) in {
        "STUDIO_PROJECTS_ROOT": ("paths", "projects_root"),
        "BLENDER_EXE": ("blender", "executable"),
        "COMFYUI_URL": ("comfyui", "base_url"),
        "OLLAMA_URL": ("ollama", "base_url"),
    }.items():
        if os.getenv(env):
            cfg[section][key] = os.environ[env]
    if os.getenv("STUDIO_ALLOW_CLOUD"):
        cfg["privacy"]["allow_cloud"] = os.environ["STUDIO_ALLOW_CLOUD"].lower() in ("1", "true", "yes")
    return cfg
