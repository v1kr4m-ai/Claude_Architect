"""Building geometry: the authoritative shape of model_spec.json (Phase 3a).

Every importer (DXF, IFC, plan image, 3D model) and the future plan generator produce a
SpecGeometry. Coordinates are metres, x east, y north, z up, origin at the plan's lower-left.
Each element carries the provenance of its geometry:
  provided  = exact values from a CAD/BIM file
  measured  = scaled from an image or mesh with a known reference size
  estimated = inferred (no known scale, generated layouts, guessed storey heights)
  unknown   = value not available
"""
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from shapely.geometry import LineString, Polygon

Prov = Literal["provided", "measured", "estimated", "unknown"]
Pt = tuple[float, float]


class Level(BaseModel):
    id: str                                   # "L0", "L1", ...
    name: str = ""
    elevation_m: float = 0.0
    height_m: float | None = None
    provenance: Prov = "unknown"


class Space(BaseModel):
    id: str
    name: str = ""
    type: str = "other"                       # brief.ROOM_TYPES where known
    level: str = "L0"
    polygon: list[Pt] = Field(default_factory=list)   # empty = position unknown (e.g. read from a label)
    area_m2: float | None = None              # computed from polygon when present
    provenance: Prov = "unknown"

    @model_validator(mode="after")
    def _area(self):
        if len(self.polygon) >= 3:
            poly = Polygon(self.polygon)
            if not poly.is_valid:
                raise ValueError(f"space {self.id}: self-intersecting polygon")
            self.area_m2 = round(poly.area, 2)
        return self


class Wall(BaseModel):
    id: str
    level: str = "L0"
    start: Pt
    end: Pt
    thickness_m: float | None = None          # None = unknown
    height_m: float | None = None
    kind: Literal["exterior", "interior", "unknown"] = "unknown"
    provenance: Prov = "unknown"

    @model_validator(mode="after")
    def _length(self):
        if LineString([self.start, self.end]).length < 0.01:
            raise ValueError(f"wall {self.id}: zero length")
        return self


class Opening(BaseModel):
    id: str
    kind: Literal["door", "window"]
    level: str = "L0"
    position: Pt                              # centre point in plan
    width_m: float | None = None
    height_m: float | None = None
    sill_m: float | None = None
    wall: str | None = None                   # host wall id when known
    provenance: Prov = "unknown"


class SpecGeometry(BaseModel):
    levels: list[Level] = Field(default_factory=list)
    spaces: list[Space] = Field(default_factory=list)
    walls: list[Wall] = Field(default_factory=list)
    openings: list[Opening] = Field(default_factory=list)
    source: dict = Field(default_factory=dict)  # {"file", "format", "units", "scale", ...}

    @model_validator(mode="after")
    def _refs(self):
        ids = {l.id for l in self.levels}
        for e in [*self.spaces, *self.walls, *self.openings]:
            if ids and e.level not in ids:
                raise ValueError(f"{e.id}: unknown level {e.level}")
        return self


def check(g: SpecGeometry) -> dict:
    """Deterministic sanity checks. Warnings only; nothing is changed."""
    warnings = []
    by_level = {}
    for s in g.spaces:
        if len(s.polygon) >= 3:
            by_level.setdefault(s.level, []).append((s, Polygon(s.polygon)))
    for lvl, items in by_level.items():
        for i, (a, pa) in enumerate(items):
            for b, pb in items[i + 1:]:
                ov = pa.intersection(pb).area
                if ov > 0.05 * min(pa.area, pb.area):
                    warnings.append(f"{lvl}: spaces {a.id} and {b.id} overlap by {ov:.1f} m2")
    for s in g.spaces:
        if s.area_m2 is not None and s.area_m2 < 1.0:
            warnings.append(f"space {s.id} '{s.name}' is under 1 m2; probably not a room")
    unknown_t = sum(1 for w in g.walls if w.thickness_m is None)
    if unknown_t:
        warnings.append(f"{unknown_t} of {len(g.walls)} walls have unknown thickness")
    if g.walls and not g.spaces:
        warnings.append("walls found but no rooms; room outlines must be added before planning")
    if not g.walls and not g.spaces:
        warnings.append("no walls and no rooms were found")
    for o in g.openings:
        if o.width_m is not None and not 0.4 <= o.width_m <= 6.0:
            warnings.append(f"{o.kind} {o.id}: width {o.width_m} m is unusual; check units")
    xs = [p[0] for w in g.walls for p in (w.start, w.end)] + [p[0] for s in g.spaces for p in s.polygon]
    ys = [p[1] for w in g.walls for p in (w.start, w.end)] + [p[1] for s in g.spaces for p in s.polygon]
    summary = {"levels": len(g.levels), "spaces": len(g.spaces), "walls": len(g.walls),
               "doors": sum(o.kind == "door" for o in g.openings),
               "windows": sum(o.kind == "window" for o in g.openings),
               "extent_m": [round(max(xs) - min(xs), 2), round(max(ys) - min(ys), 2)] if xs else None,
               "room_area_m2": round(sum(s.area_m2 or 0 for s in g.spaces), 1)}
    if summary["extent_m"] and max(summary["extent_m"]) > 500:
        warnings.append(f"plan extent {summary['extent_m']} m is implausible for a building; check units")
    if summary["extent_m"] and 0 < max(summary["extent_m"]) < 2:
        warnings.append(f"plan extent {summary['extent_m']} m is tiny; units are probably wrong")
    return {"summary": summary, "warnings": warnings}
