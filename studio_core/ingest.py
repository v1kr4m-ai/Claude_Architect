"""Reference ingestion: copy the original untouched, thumbnail it, fingerprint it, register it."""
import hashlib
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageOps

from . import config, projects
from .projects import StudioError

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
PDF_EXT = {".pdf"}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dhash(img: Image.Image) -> str:
    """64-bit difference hash; near-identical images land within a few bits of each other."""
    g = ImageOps.exif_transpose(img).convert("L").resize((9, 8), Image.LANCZOS)
    px = g.tobytes()
    bits = "".join("1" if px[r * 9 + c] > px[r * 9 + c + 1] else "0" for r in range(8) for c in range(8))
    return f"{int(bits, 2):016x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _run(cmd: list[str], timeout: int = 300) -> str:
    # list-form argv, no shell: file names can never be interpreted as commands
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise StudioError(f"{cmd[0]} failed: {r.stderr.strip()[-300:]}")
    return r.stdout


def _derive_images(src: Path, kind: str, out: Path, cfg: dict) -> list[Path]:
    """Video -> frames at a fixed interval; PDF -> one image per page."""
    out.mkdir(parents=True, exist_ok=True)
    if kind == "video":
        ic = cfg["ingest"]
        dur = float(_run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=noprint_wrappers=1:nokey=1", str(src)]).strip())
        step = max(ic["video_frame_interval_s"], dur / ic["video_max_frames"])
        _run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vf", f"fps=1/{step:.3f}",
              "-frames:v", str(ic["video_max_frames"]), "-q:v", "2", str(out / "frame_%04d.jpg")], 900)
    else:
        _run(["pdftoppm", "-r", "110", "-jpeg", str(src), str(out / "page")])
    return sorted(out.glob("*.jpg"))


def import_reference(slug: str, source: str, note: str = "", author: str = "user") -> dict:
    """Returns {"added": [...], "skipped": [...]}. Originals are copied, never moved or altered."""
    src = Path(source).expanduser().resolve()
    if not src.is_file():
        raise StudioError(f"Not a file: {source}")
    ext = src.suffix.lower()
    kind = ("image" if ext in IMAGE_EXT else "video" if ext in VIDEO_EXT else
            "pdf" if ext in PDF_EXT else None)
    if not kind:
        raise StudioError(f"Unsupported reference type: {ext}")
    cfg = config.load()
    p = projects.project_dir(slug)
    sha = _sha256(src)
    refs = projects.read_state(slug, "reference_manifest.json")["references"]
    dup = next((r for r in refs if r["sha256"] == sha), None)
    if dup:
        return {"added": [], "skipped": [{"file": src.name, "reason": f"exact duplicate of {dup['id']}"}]}

    n = max([int(r["id"][3:]) for r in refs if r["id"].count("-") == 0] + [0]) + 1
    rid = f"REF{n:03d}"
    dest = p / "references" / "originals" / f"{rid}{ext}"
    shutil.copy2(src, dest)
    parent = {"id": rid, "kind": kind, "source_name": src.name, "original": dest.relative_to(p).as_posix(),
              "sha256": sha, "bytes": dest.stat().st_size, "note": note, "imported": projects.now(),
              "analysis": None}
    added, skipped = [parent], []

    if kind == "image":
        images = [(parent, dest)]
    else:
        frames = _derive_images(dest, kind, p / "references" / "frames" / rid, cfg)
        images = [({"id": f"{rid}-{i:02d}", "kind": "frame" if kind == "video" else "page",
                    "parent": rid, "source_name": f.name, "original": f.relative_to(p).as_posix(),
                    "sha256": _sha256(f), "bytes": f.stat().st_size, "note": "", "imported": projects.now(),
                    "analysis": None}, f) for i, f in enumerate(frames, 1)]
        parent["derived"] = len(images)
        added += [e for e, _ in images]

    seen = [(r["id"], r["dhash"]) for r in refs if r.get("dhash")]
    for entry, f in images:
        try:
            with Image.open(f) as im:
                entry.update(width=im.width, height=im.height, format=im.format, dhash=dhash(im))
                thumb = p / "references" / "thumbnails" / f"{entry['id']}.jpg"
                t = ImageOps.exif_transpose(im).convert("RGB")
                t.thumbnail((cfg["ingest"]["thumbnail_px"],) * 2)
                t.save(thumb, "JPEG", quality=85)
                entry["thumbnail"] = thumb.relative_to(p).as_posix()
        except OSError as e:
            if entry is parent:  # a corrupt upload must not be registered
                dest.unlink(missing_ok=True)
                raise StudioError(f"Cannot read image {src.name}: {e}")
            continue
        near = next((i for i, h in seen if hamming(h, entry["dhash"])
                     <= cfg["ingest"]["near_duplicate_hamming"]), None)
        if near:
            # kept and flagged, not dropped: the user decides whether a near-duplicate matters
            entry["near_duplicate_of"] = near
        seen.append((entry["id"], entry["dhash"]))

    projects.update_state(slug, "reference_manifest.json", lambda d: d["references"].extend(added),
                          author, f"import {src.name} as {rid}")
    return {"added": added, "skipped": skipped}
