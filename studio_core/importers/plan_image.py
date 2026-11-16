"""Importer for a photo, scan or PDF of a floor plan (png jpg jpeg webp pdf).

What it does
  * Walls: OpenCV. Dark ink -> thick strokes only (thin text, dimension lines and hatching are
    removed) -> long horizontal / vertical runs -> one wall centreline per run. Walls only get
    coordinates when a real width is known (`width_m` option, or an overall width written on the
    drawing); without it the wall count is only reported in the notes.
  * Rooms: a vision model lists the room labels and the dimensions written on the drawing, a JSON
    model structures them, and Python parses the feet-inches / metres text itself.

Known limits
  * Room positions are NOT extracted: spaces have no polygon. Area is only set when the drawing
    states both dimensions of the room.
  * Walls are axis-aligned only; diagonal and curved walls are dropped. Doors and windows are not read.
  * Scale comes from one overall width, so a skewed photo (perspective) gives skewed results.
  * PDF: page 1 only, via `pdftoppm` (poppler) which must be on PATH.
"""
import re
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps
from pydantic import BaseModel, Field, ValidationError

from .. import brief, config, geometry, providers, router, vision
from ..projects import StudioError

FT = 0.3048
IN = 0.0254

SYSTEM = ("You read architectural floor plans. Report only text that is written on the drawing. "
          "Never estimate or invent a size.")
DESCRIBE = ("List every room label you can read on this floor plan, one per line. After each label copy "
            "any dimensions written for that room exactly as written (for example 12'0\" x 10'6\" or "
            "3.6 x 3.0). If the overall building dimensions are written, report them exactly as written "
            "too. If a room has no written dimensions say 'no dimensions'.")
STRUCTURE = ("Convert the description of a floor plan below into the JSON schema. Copy the written "
             "dimensions into written_dims exactly as written (empty string if none). Leave width_m and "
             "length_m as 0 unless the description gives them in metres. overall_written is the overall "
             "building size exactly as written, or an empty string. Use only what the description states."
             "\n\nDESCRIPTION:\n")


class _Room(BaseModel):
    name: str
    written_dims: str = ""
    width_m: float | None = None
    length_m: float | None = None


class _Plan(BaseModel):
    rooms: list[_Room] = Field(default_factory=list)
    overall_written: str = ""
    overall_width_m: float | None = None
    overall_length_m: float | None = None


# ---------- written dimensions (deterministic; never trust model arithmetic) ----------
_FEET = re.compile(r"""(\d+(?:\.\d+)?)\s*(?:'|′|ft|feet)\s*(?:-\s*)?(?:(\d+(?:\.\d+)?)\s*(?:"|″|''|in)?)?""")


def parse_length(text: str) -> float | None:
    """One written length -> metres. 12'6", 12' 6", 12'-6", 12', 3.6m, 3600mm, 3.6 (assumed metres)."""
    t = text.strip().lower().replace(",", ".")
    m = _FEET.fullmatch(t)
    if m:
        return round(float(m[1]) * FT + float(m[2] or 0) * IN, 4)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*mm", t)
    if m:
        return float(m[1]) / 1000
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*cm", t)
    if m:
        return float(m[1]) / 100
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:m|mtr|meters?|metres?)?", t)
    if m and 0 < float(m[1]) <= 100:  # bare number: metres, if it is a plausible room/building size
        return float(m[1])
    return None


def parse_dims(text: str) -> tuple[float, float] | None:
    """'12'0" x 10'6"' or '3.6 x 3.0' -> (width_m, length_m); None unless both sides parse."""
    parts = re.split(r"\s*(?:x|×|\bby\b)\s*", (text or "").strip().lower())
    if len(parts) != 2:
        return None
    a, b = parse_length(parts[0]), parse_length(parts[1])
    return (a, b) if a and b else None


_DIMS_IN_TEXT = re.compile(r"\d[\d'\"’”.\s\-]*(?:mm|cm|m\b)?\s*(?:x|×|by)\s*\d[\d'\"’”.\s\-]*(?:mm|cm|m\b)?", re.I)


def find_dims(text: str) -> tuple[float, float] | None:
    """First 'A x B' size inside free text (e.g. a vision model's sentence), parsed strictly."""
    m = _DIMS_IN_TEXT.search(text or "")
    return parse_dims(m[0].replace("’", "'").replace("”", '"').strip(" -")) if m else None


room_type = brief.room_type  # single shared mapping


# ---------- image handling ----------
def _load(path: Path, tmp: Path) -> Path:
    if path.suffix.lower() != ".pdf":
        return path
    try:
        subprocess.run(["pdftoppm", "-r", "150", "-png", "-f", "1", "-l", "1", str(path), str(tmp / "page")],
                       check=True, capture_output=True, timeout=120)
    except FileNotFoundError as e:
        raise StudioError("PDF import needs 'pdftoppm' (poppler) on PATH") from e
    except (subprocess.SubprocessError, OSError) as e:
        raise StudioError(f"pdftoppm failed on {path.name}: {e}") from e
    pages = sorted(tmp.glob("page*.png"))
    if not pages:
        raise StudioError(f"pdftoppm produced no page for {path.name}")
    return pages[0]


def find_walls(img_path: Path) -> tuple[list[dict], tuple[int, int]]:
    """Axis-aligned wall centrelines in pixels: [{"a": (x,y), "b": (x,y), "t": thickness_px}]."""
    with Image.open(img_path) as im:
        gray = np.array(ImageOps.exif_transpose(im).convert("L"))
    h, w = gray.shape
    ink = cv2.threshold(cv2.GaussianBlur(gray, (3, 3), 0), 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    k = max(4, min(h, w) // 150)                       # thicker than text / dimension lines / hatching
    thick = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((k, k), np.uint8))
    run = max(20, min(h, w) // 12)                     # a wall is a long straight run
    dist = cv2.distanceTransform(thick, cv2.DIST_L2, 3)
    segs = []
    for horizontal, kern in ((True, (run, 1)), (False, (1, run))):
        mask = cv2.morphologyEx(thick, cv2.MORPH_OPEN, np.ones(kern[::-1], np.uint8))
        n, lab, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        for i in range(1, n):
            x, y, cw, ch, _a = stats[i]
            length = cw if horizontal else ch
            if length < run:
                continue
            t = float(2 * dist[lab == i].max())
            if horizontal:
                segs.append({"a": (x, y + ch / 2), "b": (x + cw, y + ch / 2), "t": t})
            else:
                segs.append({"a": (x + cw / 2, y), "b": (x + cw / 2, y + ch), "t": t})
    return segs, (w, h)


# ---------- rooms (vision then json, like vision.py) ----------
def read_rooms(img_path: Path, vision_model, provider, model, json_provider, json_model) -> _Plan:
    cfg = config.load()
    if provider is None:
        v, j = router.pick("vision", cfg=cfg, override=vision_model), router.pick("json", cfg=cfg)
        provider, model = providers.get(v["model"].provider), v["model"].name
        json_provider, json_model = providers.get(j["model"].provider), j["model"].name
    elif json_provider is None:
        json_provider, json_model = provider, model
    image = vision._prepare(img_path, cfg["vision"]["max_image_px"])
    description = provider.chat(model, DESCRIBE, images=[image], system=SYSTEM).strip()
    if hasattr(provider, "unload"):
        provider.unload(model)
    if not description:
        raise StudioError("vision model returned nothing for the plan image")
    schema, prompt, err, plan = _Plan.model_json_schema(), STRUCTURE + description, "", None
    for _ in range(cfg["vision"]["retries"] + 1):
        raw = json_provider.chat(json_model, prompt, schema=schema)
        try:
            s, e = raw.find("{"), raw.rfind("}")
            plan = _Plan.model_validate_json(raw[s:e + 1])
            break
        except (ValueError, ValidationError) as ex:
            err = str(ex)[:400]
            prompt = STRUCTURE + description + f"\n\nYour previous answer was rejected: {err}\nReturn valid JSON only."
    if hasattr(json_provider, "unload"):
        json_provider.unload(json_model)
    if plan is None:
        raise StudioError(f"room list failed validation: {err}")
    return plan, description


def run(path: Path, **opts) -> tuple[geometry.SpecGeometry, list[str]]:
    width_m = opts.get("width_m")
    level = opts.get("level") or "L0"
    notes = ["Room positions are not extracted from images; rooms have no outline.",
             "Walls are axis-aligned only; diagonal or curved walls are dropped. Doors and windows are not read."]
    with tempfile.TemporaryDirectory() as td:
        img = _load(Path(path), Path(td))
        try:
            segs, (w_px, h_px) = find_walls(img)
        except (OSError, cv2.error) as e:
            raise StudioError(f"cannot read image {Path(path).name}: {e}") from e
        plan, description = read_rooms(img, opts.get("vision_model"), opts.get("provider"), opts.get("model"),
                          opts.get("json_provider"), opts.get("json_model"))

    spaces = []
    for i, r in enumerate(plan.rooms, 1):
        dims = parse_dims(r.written_dims)
        if not dims:
            # the structuring model sometimes drops sizes the vision model did read (seen live):
            # parse them from the vision model's own line for this room, in code
            line = next((ln for ln in description.splitlines() if r.name.lower() in ln.lower()), "")
            dims = find_dims(line.lower().split(r.name.lower(), 1)[-1]) if line else None
            if dims:
                notes.append(f"room '{r.name}': size taken from the vision description line '{line.strip()[:80]}'")
        spaces.append(geometry.Space(
            id=f"S{i:03d}", name=r.name, type=room_type(r.name), level=level,
            area_m2=round(dims[0] * dims[1], 2) if dims else None,
            provenance="provided" if dims else "unknown"))
        if not dims:
            notes.append(f"room '{r.name}': no readable dimensions, area unknown")

    scale, scale_from = None, None
    if width_m:
        scale_from = "width_m option"
    else:  # overall width written on the drawing (deterministic parse of the text, not the model's number)
        drawn = parse_dims(plan.overall_written)
        drawn_w = drawn[0] if drawn else parse_length(plan.overall_written)
        if drawn_w:
            width_m, scale_from = drawn_w, "overall width written on the drawing"
    walls = []
    if segs and width_m:
        xs = [p[0] for s in segs for p in (s["a"], s["b"])]
        ys = [p[1] for s in segs for p in (s["a"], s["b"])]
        x0, x1, y1 = min(xs), max(xs), max(ys)
        scale = width_m / (x1 - x0)
        for s in sorted(segs, key=lambda s: (s["a"][1], s["a"][0])):
            a = (round((s["a"][0] - x0) * scale, 3), round((y1 - s["a"][1]) * scale, 3))  # y up
            b = (round((s["b"][0] - x0) * scale, 3), round((y1 - s["b"][1]) * scale, 3))
            edge = max(s["t"], 2) * scale * 1.5
            on_edge = (a[1] == b[1] and (a[1] <= edge or a[1] >= (max(ys) - min(ys)) * scale - edge)) or \
                      (a[0] == b[0] and (a[0] <= edge or a[0] >= width_m - edge))
            walls.append(geometry.Wall(id=f"W{len(walls) + 1:03d}", level=level, start=a, end=b,
                                       thickness_m=round(s["t"] * scale, 3),
                                       kind="exterior" if on_edge else "interior", provenance="measured"))
        notes.append(f"{len(walls)} wall lines scaled with {scale_from} = {width_m} m "
                     f"({scale * 1000:.1f} mm per pixel)")
    elif segs:
        notes.append(f"{len(segs)} wall lines found; give width_m to scale them (no walls output)")
    else:
        notes.append("no wall lines found in the image")

    geo = geometry.SpecGeometry(
        levels=[geometry.Level(id=level, name="Ground floor" if level == "L0" else level)],
        spaces=spaces, walls=walls,
        source={"units": "m", "scale": {"m_per_px": scale, "basis": scale_from} if scale else None,
                "image_px": [w_px, h_px],
                # audit trail: what the vision model said and what was read as written sizes
                "vision_description": description,
                "read": {"rooms": [{"name": r.name, "written_dims": r.written_dims} for r in plan.rooms],
                         "overall_written": plan.overall_written}})
    return geo, notes
