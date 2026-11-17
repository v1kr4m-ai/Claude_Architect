"""ComfyUI client and the render-enhancement workflow (Phase 5).

Enhancement = img2img over a Blender render, held to the building's geometry by a line
ControlNet fed with Blender's own line pass. The Blender render stays the authoritative image;
the enhanced one is stored beside it, flagged non-authoritative, with a geometry score.

Queue etiquette: ComfyUI may be busy with the user's other work. This client only ever queues
its own prompt, waits its turn, and on timeout removes only its own prompt. It never clears the
queue and never interrupts a running job.
"""
import time
import uuid
from pathlib import Path

import cv2
import httpx
import numpy as np

from . import config
from .projects import StudioError

NEGATIVE = ("cartoon, sketch, drawing, cgi, 3d render, lowres, blurry, distorted geometry, warped walls, "
            "extra windows, extra doors, extra floors, people, cars, text, watermark, logo")
STYLE = {"day": "bright clear daylight, soft shadows", "golden": "golden hour, warm low sun, long shadows",
         "overcast": "overcast soft diffuse light", "dusk": "blue hour dusk, warm glowing interior lights"}


def prompt_for(brief_prefs: dict, preset: str, interior: str | None = None) -> str:
    mats = ", ".join(m for m in (brief_prefs.get("materials") or []) if isinstance(m, str))
    style = brief_prefs.get("style") or "modern"
    if interior:
        return (f"professional interior photograph of a {interior} in a {style} house, natural materials, "
                f"{STYLE.get(preset, '')}, photorealistic, architectural digest, 8k")
    return (f"professional architectural photograph of a {style} house exterior, {mats}, landscaped garden, "
            f"{STYLE.get(preset, '')}, photorealistic, sharp focus, 8k")


def workflow(image: str, lines: str, positive: str, cfg: dict, seed: int, prefix: str) -> dict:
    """API-format graph. Node classes verified against the running server by check_nodes()."""
    c = cfg["comfyui"]["enhance"]
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": c["checkpoint"]}},
        "2": {"class_type": "LoadImage", "inputs": {"image": image}},
        "3": {"class_type": "LoadImage", "inputs": {"image": lines}},
        # Blender's line pass is black on white; line ControlNets expect white lines on black
        "4": {"class_type": "ImageInvert", "inputs": {"image": ["3", 0]}},
        "5": {"class_type": "ControlNetLoader", "inputs": {"control_net_name": c["controlnet"]}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": positive, "clip": ["1", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": NEGATIVE, "clip": ["1", 1]}},
        "8": {"class_type": "ControlNetApplyAdvanced",
              "inputs": {"positive": ["6", 0], "negative": ["7", 0], "control_net": ["5", 0], "image": ["4", 0],
                         "strength": c["control_strength"], "start_percent": 0.0, "end_percent": c["control_end"],
                         "vae": ["1", 2]}},
        "9": {"class_type": "VAEEncode", "inputs": {"pixels": ["2", 0], "vae": ["1", 2]}},
        "10": {"class_type": "KSampler",
               "inputs": {"model": ["1", 0], "positive": ["8", 0], "negative": ["8", 1], "latent_image": ["9", 0],
                          "seed": seed, "steps": c["steps"], "cfg": c["cfg"], "sampler_name": c["sampler"],
                          "scheduler": c["scheduler"], "denoise": c["denoise"]}},
        "11": {"class_type": "VAEDecode", "inputs": {"samples": ["10", 0], "vae": ["1", 2]}},
        "12": {"class_type": "SaveImage", "inputs": {"images": ["11", 0], "filename_prefix": prefix}},
    }


class Comfy:
    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or config.load()
        self.base = self.cfg["comfyui"]["base_url"].rstrip("/")
        self.timeout = self.cfg["comfyui"]["timeout_seconds"]
        self.client_id = uuid.uuid4().hex

    def up(self) -> bool:
        try:
            return httpx.get(f"{self.base}/system_stats", timeout=3).status_code == 200
        except httpx.HTTPError:
            return False

    def check_nodes(self, graph: dict) -> list[str]:
        """Problems that would make the graph fail: missing node classes, missing model files."""
        info = httpx.get(f"{self.base}/object_info", timeout=60).json()
        problems = []
        for node in graph.values():
            cls = node["class_type"]
            if cls not in info:
                problems.append(f"node class not installed: {cls}")
                continue
            required = info[cls]["input"].get("required", {})
            for key in ("ckpt_name", "control_net_name"):
                if key in node["inputs"] and key in required:
                    options = required[key][0] if isinstance(required[key][0], list) else \
                        (required[key][1] or {}).get("options", [])
                    if node["inputs"][key] not in options:
                        problems.append(f"{key} '{node['inputs'][key]}' not found; installed: {options[:8]}")
        return problems

    def upload(self, path: Path, name: str) -> str:
        with path.open("rb") as f:
            r = httpx.post(f"{self.base}/upload/image", files={"image": (name, f, "image/png")},
                           data={"overwrite": "true", "subfolder": "architectural_studio"}, timeout=120)
        r.raise_for_status()
        j = r.json()
        return f"{j['subfolder']}/{j['name']}" if j.get("subfolder") else j["name"]

    def run(self, graph: dict) -> list[dict]:
        """Queue one prompt, wait for it, return its output image records."""
        try:
            r = httpx.post(f"{self.base}/prompt", json={"prompt": graph, "client_id": self.client_id}, timeout=120)
        except httpx.HTTPError as e:
            raise StudioError(f"ComfyUI did not accept the prompt ({type(e).__name__})") from e
        if r.status_code != 200:
            raise StudioError(f"ComfyUI rejected the workflow: {r.text[:600]}")
        pid = r.json()["prompt_id"]
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                h = httpx.get(f"{self.base}/history/{pid}", timeout=30).json().get(pid)
            except httpx.HTTPError:
                # ComfyUI stops answering while it loads a large model (seen live with ~1 GB RAM
                # free); that is "busy", not "failed": keep waiting until the deadline
                time.sleep(5)
                continue
            if h:
                status = h.get("status", {})
                if status.get("status_str") == "error":
                    msgs = [m[1].get("exception_message", "") for m in status.get("messages", []) if m[0] == "execution_error"]
                    raise StudioError(f"ComfyUI workflow failed: {' '.join(msgs)[:500] or 'unknown error'}")
                if status.get("completed", True):
                    return [img for out in h.get("outputs", {}).values() for img in out.get("images", [])]
            time.sleep(1.5)
        # timed out: remove only our own prompt if it is still waiting; never touch anything else
        try:
            httpx.post(f"{self.base}/queue", json={"delete": [pid]}, timeout=30)
        except httpx.HTTPError:
            pass
        raise StudioError(f"ComfyUI did not finish within {self.timeout} s (its queue may be busy); "
                          "our prompt was withdrawn, nothing else was touched")

    def fetch(self, img: dict, dest: Path) -> Path:
        r = httpx.get(f"{self.base}/view", params={"filename": img["filename"], "subfolder": img.get("subfolder", ""),
                                                   "type": img.get("type", "output")}, timeout=120)
        r.raise_for_status()
        dest.write_bytes(r.content)
        return dest

    def free(self) -> None:
        """Ask ComfyUI to unload models so Blender/Ollama get the VRAM back."""
        try:
            httpx.post(f"{self.base}/free", json={"unload_models": True, "free_memory": True}, timeout=30)
        except httpx.HTTPError:
            pass


def _edges(gray):
    """Canny with thresholds from the image's own median after a global 1-99% stretch, so a dark
    interior and a hazy aerial are measured as fairly as a crisp facade. (A local contrast boost
    (CLAHE) was tried and rejected: it turned grass texture into edges and let an altered roof pass.)"""
    g = cv2.GaussianBlur(gray, (5, 5), 0)
    lo, hi = np.percentile(g, (1, 99))
    g = np.clip((g - lo) * 255.0 / max(float(hi - lo), 1.0), 0, 255).astype(np.uint8)
    m = float(np.median(g))
    return cv2.Canny(g, int(max(0, 0.66 * m)), int(min(255, 1.33 * m))) > 0


def geometry_score(lines_png: Path, enhanced_png: Path, render_png: Path | None = None,
                   overlay_png: Path | None = None) -> dict:
    """Did enhancement keep the building's geometry?

    edge_recall: share of Blender line-pass pixels with an edge nearby in the enhanced image.
        Catches missing or moved walls, windows, roof edges. Reliable.
    unexplained_share / unexplained_segments: long straight lines in the enhanced image that lie on
        neither a model edge nor an edge of the Blender render itself (which explains shadows).
        Points at ADDED structure (a new ridge, an invented window), but also at harmless
        additions (mullions, floorboards); it is a pointer for human review, drawn in red on
        overlay_png, not a reliable verdict on its own. Measured live: a hip roof the AI added
        on a flat-roofed model was found as one red segment but did not move the share much.
    """
    lines = cv2.imread(str(lines_png), cv2.IMREAD_GRAYSCALE)
    colour = cv2.imread(str(enhanced_png))
    if lines is None or colour is None:
        raise StudioError("cannot read images for the geometry check")
    if colour.shape[:2] != lines.shape:
        colour = cv2.resize(colour, (lines.shape[1], lines.shape[0]), interpolation=cv2.INTER_AREA)
    enh = cv2.cvtColor(colour, cv2.COLOR_BGR2GRAY)
    truth = lines < 128
    if truth.sum() < 50:
        raise StudioError("line pass has no lines; cannot check geometry")
    k = max(3, int(round(lines.shape[1] * 0.006)) | 1)
    ker = np.ones((k, k), np.uint8)
    edges = _edges(enh)
    recall = float((truth & (cv2.dilate(edges.astype(np.uint8), ker) > 0)).sum() / truth.sum())

    allowed_src = truth.copy()
    if render_png is not None:
        r = cv2.imread(str(render_png), cv2.IMREAD_GRAYSCALE)
        if r is not None:
            allowed_src |= _edges(cv2.resize(r, (lines.shape[1], lines.shape[0])))
    allowed = cv2.dilate(allowed_src.astype(np.uint8), ker) > 0
    segs = cv2.HoughLinesP(edges.astype(np.uint8) * 255, 1, np.pi / 180, 60,
                           minLineLength=int(0.04 * lines.shape[1]), maxLineGap=4)
    total = off = 0
    flagged = []
    for x1, y1, x2, y2 in (segs.reshape(-1, 4).tolist() if segs is not None else []):
        n = max(abs(x2 - x1), abs(y2 - y1)) + 1
        xs, ys = np.linspace(x1, x2, n).astype(int), np.linspace(y1, y2, n).astype(int)
        bad = int((~allowed[ys, xs]).sum())
        total, off = total + n, off + bad
        if bad > 0.5 * n:
            flagged.append([x1, y1, x2, y2])
    if overlay_png is not None:
        vis = colour.copy()
        for x1, y1, x2, y2 in flagged:
            cv2.line(vis, (x1, y1), (x2, y2), (0, 0, 255), 3)
        cv2.imwrite(str(overlay_png), vis)
    return {"edge_recall": round(recall, 3), "unexplained_share": round(off / total, 3) if total else 0.0,
            "unexplained_segments": len(flagged), "tolerance_px": k}
