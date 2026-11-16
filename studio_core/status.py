"""Live environment probe. Reports what is actually reachable right now, nothing assumed."""
import functools
import shutil
import subprocess
from pathlib import Path

import httpx

from . import config, providers, router


def blender(cfg: dict) -> dict:
    exe = cfg["blender"]["executable"]
    if not exe or not Path(exe).is_file():
        return {"ok": False, "detail": "blender.executable not set or not found in config/studio.yaml"}
    return _blender_version(exe)


@functools.lru_cache(maxsize=4)  # launching Blender costs seconds; the version cannot change while running
def _blender_version(exe: str) -> dict:
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60).stdout
        return {"ok": True, "executable": exe, "version": out.splitlines()[0].strip()}
    except (OSError, subprocess.SubprocessError, IndexError) as e:
        return {"ok": False, "detail": str(e)}


def comfyui(cfg: dict) -> dict:
    try:
        s = httpx.get(cfg["comfyui"]["base_url"].rstrip("/") + "/system_stats", timeout=3).json()
        return {"ok": True, "version": s.get("system", {}).get("comfyui_version"),
                "devices": [d.get("name") for d in s.get("devices", [])]}
    except (httpx.HTTPError, ValueError):
        return {"ok": False, "detail": f"not reachable at {cfg['comfyui']['base_url']} (start it to use Phase 5)"}


def report() -> dict:
    cfg = config.load()
    inv = providers.inventory()
    provs = {p.name: {"available": any(m.provider == p.name for m in inv),
                      "models": [m.to_dict() for m in inv if m.provider == p.name]}
             for p in providers.all_providers()}
    return {"gpu": router.gpu(), "blender": blender(cfg), "comfyui": comfyui(cfg),
            "ffmpeg": bool(shutil.which("ffmpeg")), "pdftoppm": bool(shutil.which("pdftoppm")),
            "privacy": cfg["privacy"], "projects_root": cfg["paths"]["projects_root"], "providers": provs}
