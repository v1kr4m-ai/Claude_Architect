"""Capability-based model routing. Local first; cloud only when the privacy policy allows it."""
import subprocess

from . import config
from .providers import ModelInfo, ProviderError, inventory

# task -> capabilities a model must report to be eligible
TASKS = {
    "vision": {"vision"},
    "reasoning": {"completion"},
    "coding": {"completion"},
    "json": {"json"},
    "light": {"completion"},
    "docs": {"completion"},
    "embedding": {"embedding"},
}


def gpu() -> dict | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=True).stdout.strip().splitlines()[0]
        name, total, free = [x.strip() for x in out.split(",")]
        return {"name": name, "total_mb": int(total), "free_mb": int(free)}
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def pick(task: str, models: list[ModelInfo] | None = None, cfg: dict | None = None,
         free_vram_mb: int | None = None) -> dict:
    """Return {"model": ModelInfo, "reason": str, "rejected": [...]} or raise ProviderError."""
    if task not in TASKS:
        raise ProviderError(f"Unknown task: {task}")
    cfg = cfg or config.load()
    models = inventory() if models is None else models
    if free_vram_mb is None:
        g = gpu()
        free_vram_mb = g["free_mb"] if g else 0
    budget = free_vram_mb - cfg["hardware"]["vram_headroom_mb"]
    allow_cloud = cfg["privacy"]["allow_cloud"]
    rejected, ok = [], []
    for m in models:
        if not TASKS[task] <= m.capabilities:
            continue
        if not m.local and not allow_cloud:
            rejected.append(f"{m.id}: cloud model, privacy.allow_cloud is off")
            continue
        ok.append(m)
    if not ok:
        raise ProviderError(f"No model available for task '{task}'. Rejected: {rejected or 'none eligible'}")
    prefs = cfg["routing"].get(task, [])

    def rank(m: ModelInfo):
        # ponytail: size_mb is weights-on-disk; loaded size is larger (llama3.2-vision: 7.4 GB file,
        # 12 GB loaded). Upgrade: cache real size from /api/ps after first load and rank on that.
        fits = (not m.local) or m.size_mb <= budget
        pref = prefs.index(m.id) if m.id in prefs else len(prefs)
        # routing prefs are measured choices (MODEL_INVENTORY.md) and win outright;
        # among unlisted models: fully-on-GPU first, then bigger = usually better
        return (pref, 0 if fits else 1, -m.size_mb)

    best = sorted(ok, key=rank)[0]
    fits = (not best.local) or best.size_mb <= budget
    reason = (f"task={task}; needs {sorted(TASKS[task])}; "
              f"{'cloud' if not best.local else f'{best.size_mb} MB vs {budget} MB VRAM budget'}; "
              f"{'fits GPU' if fits else 'will spill to CPU/RAM (slow)'}; "
              f"{'listed in routing prefs' if best.id in prefs else 'not in prefs, largest eligible'}")
    return {"model": best, "reason": reason, "rejected": rejected}
