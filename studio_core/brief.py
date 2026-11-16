"""Design brief proposals (Design Architect + Planning Architect, Phase 2).

Flow:  requirements text + reference analyses --(reasoning model)--> proposal
       --(deterministic checks)--> proposals/P###.json + .md   [status: proposed]
       --(user approves, picks a concept)--> design_brief.yaml   [backup + revision]

A model never writes the brief. Numbers the model says came from the user are verified
against the requirements text; anything it cannot point to is downgraded to 'estimated'.
"""
import json
import re
from collections import defaultdict
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from . import config, projects, providers, router
from .projects import StudioError, dim

ROOM_TYPES = ("living", "dining", "kitchen", "bedroom", "master_bedroom", "bathroom", "wc", "study",
              "utility", "storage", "stair", "entrance", "corridor", "garage", "balcony", "terrace",
              "pooja", "other")
# Rule-of-thumb minimum net areas (m2) for a sanity warning. NOT building-code values.
MIN_AREA = {"living": 12, "dining": 8, "kitchen": 6, "bedroom": 8, "master_bedroom": 11, "bathroom": 3,
            "wc": 1.2, "study": 5, "utility": 2.5, "stair": 4, "garage": 14, "entrance": 2}
OUTDOOR = {"balcony", "terrace"}
SYNONYMS = ((("parking", "car", "carport"), "garage"), (("wardrobe", "closet", "store"), "storage"),
            (("lounge", "family", "drawing"), "living"), (("toilet", "powder"), "wc"),
            (("bath",), "bathroom"), (("puja", "prayer", "mandir"), "pooja"), (("staircase",), "stair"),
            (("foyer", "lobby"), "entrance"), (("laundry", "wash"), "utility"),
            (("office", "library"), "study"), (("hall", "passage", "landing"), "corridor"))
GROSS_FACTOR = 1.30  # net room area -> gross built area (circulation ~18% + walls ~12%); heuristic


# ---------- what the model must return ----------
class PNumber(BaseModel):
    value: float | None = None
    source: Literal["user", "suggested", "unknown"] = "unknown"


class PRoom(BaseModel):
    name: str = Field(description="unique, e.g. 'Bedroom 2'")
    type: str = Field(description="one of: " + ", ".join(ROOM_TYPES))
    level: int = Field(0, ge=0, description="0 = ground floor")
    area_m2: float = Field(gt=0)
    area_source: Literal["user", "suggested"] = Field(description="'user' only if the requirements state this area")
    adjacent_to: list[str] = Field(default_factory=list, description="names of rooms it should connect to")
    notes: str = ""

    @field_validator("type", mode="before")
    @classmethod
    def _known_type(cls, v):
        v = str(v).strip().lower().replace(" ", "_")
        if v in ROOM_TYPES:
            return v
        return next((t for words, t in SYNONYMS if any(w in v for w in words)), "other")


class PConcept(BaseModel):
    name: str
    massing: str
    roof: str
    facade_materials: list[str]
    tradeoffs: str


class PInfluence(BaseModel):
    reference: str = Field(description="reference id such as REF001")
    takes: str = Field(description="what this design takes from it")


class ProposalLLM(BaseModel):
    summary: str
    building_type: str
    floors: int = Field(ge=1, le=10)
    style: str
    plot_width_m: PNumber
    plot_length_m: PNumber
    target_gross_area_m2: PNumber
    rooms: list[PRoom] = Field(min_length=1)
    concepts: list[PConcept] = Field(min_length=1, max_length=3)
    reference_influences: list[PInfluence] = Field(default_factory=list)
    assumptions: list[str]
    open_questions: list[str]


SYSTEM = (
    "You are the design and planning architect of a residential design studio. From the client's "
    "requirements and reference analyses, propose a design brief. Rules: include every room the "
    "client asks for; mark a number as source 'user' only when the client's text states it, otherwise "
    "'suggested' (or 'unknown' if you cannot reasonably suggest one); give realistic net room areas in "
    "square metres; place rooms on levels (0 = ground) consistent with the floor count and include a "
    "stair on every level when there is more than one floor; list adjacencies by exact room name; give "
    "1 to 3 genuinely different concept options; record every assumption and every question the client "
    "must answer. Do not claim compliance with any building code.")


# ---------- deterministic checks (Planning Architect tools) ----------
_NUM = r"(\d[\d,]*\.?\d*)"
# An area counts as stated only if written with an area unit; a length only with a length unit or
# as one side of "12 x 20". Stops "12" in "12 x 20 m plot" from verifying a 12 m2 bedroom.
_AREA_RE = re.compile(_NUM + r"\s*(?:m2|m²|sq\.?\s*m|sqm|square\s*met)", re.I)
_LEN_RE = re.compile(_NUM + r"\s*(?:m\b|metres?\b|meters?\b|x\b|×)|(?:x|×)\s*" + _NUM, re.I)


def _numbers_in(text: str) -> dict[str, list[float]]:
    num = lambda m: next(float(g.replace(",", "")) for g in m.groups() if g)
    return {"area": [num(m) for m in _AREA_RE.finditer(text)],
            "length": [num(m) for m in _LEN_RE.finditer(text)]}


def _provenance(value, source, text_numbers, kind) -> tuple[str, str]:
    if value is None:
        return "unknown", ""
    if source == "user":
        if any(abs(value - n) < 0.01 for n in text_numbers[kind]):
            return "provided", "stated in requirements"
        return "estimated", f"model said user-stated, but no matching {kind} is in the requirements"
    return "estimated", "suggested by model"


def check(p: ProposalLLM, requirements: str, known_refs: set[str]) -> dict:
    """Pure function: proposal in, verified rooms + warnings + area summary out."""
    nums = _numbers_in(requirements)
    warnings, rooms = [], []
    names = [r.name for r in p.rooms]
    for n in {n for n in names if names.count(n) > 1}:
        warnings.append(f"duplicate room name: {n}")
    name_set = set(names)

    edges = set()
    for r in p.rooms:
        prov, note = _provenance(r.area_m2, r.area_source, nums, "area")
        rooms.append({"name": r.name, "type": r.type, "level": r.level,
                      "area_m2": dim(round(r.area_m2, 2), prov, note),
                      "adjacent_to": [a for a in r.adjacent_to if a in name_set and a != r.name],
                      "notes": r.notes})
        for a in r.adjacent_to:
            if a not in name_set:
                warnings.append(f"{r.name}: adjacency to unknown room '{a}' dropped")
            elif a != r.name:
                edges.add(tuple(sorted((r.name, a))))
        if r.level >= p.floors:
            warnings.append(f"{r.name}: on level {r.level} but the building has {p.floors} floor(s)")
        floor_min = MIN_AREA.get(r.type)
        if floor_min and r.area_m2 < floor_min:
            warnings.append(f"{r.name}: {r.area_m2} m2 is below the {floor_min} m2 rule of thumb for a {r.type}")

    # adjacency graph: isolated rooms and disconnected groups per level
    graph = defaultdict(set)
    for a, b in edges:
        graph[a].add(b)
        graph[b].add(a)
    for r in p.rooms:
        if r.type not in OUTDOOR and not graph[r.name]:
            warnings.append(f"{r.name}: no adjacency, so no way to reach it is defined")
    if p.floors > 1:
        for lvl in range(p.floors):
            if not any(r.type == "stair" and r.level == lvl for r in p.rooms):
                warnings.append(f"level {lvl}: no stair")

    per_level = defaultdict(float)
    for r in p.rooms:
        if r.type not in OUTDOOR:
            per_level[r.level] += r.area_m2
    net = sum(per_level.values())
    gross = round(net * GROSS_FACTOR, 1)
    summary = {"net_m2_by_level": {str(k): round(v, 1) for k, v in sorted(per_level.items())},
               "net_m2": round(net, 1), "gross_m2_estimate": gross,
               "gross_factor": GROSS_FACTOR, "adjacency_edges": len(edges)}
    tgt = p.target_gross_area_m2.value
    if tgt and abs(gross - tgt) / tgt > 0.15:
        warnings.append(f"estimated gross {gross} m2 differs from target {tgt} m2 by more than 15%")
    w, l = p.plot_width_m.value, p.plot_length_m.value
    if w and l:
        ground = round(per_level.get(0, 0) * GROSS_FACTOR, 1)
        summary["plot_m2"] = round(w * l, 1)
        summary["ground_coverage_pct"] = round(100 * ground / (w * l), 1)
        if ground > w * l:
            warnings.append(f"ground floor (~{ground} m2 gross) is larger than the plot ({w * l:.0f} m2)")
        elif ground > 0.75 * w * l:
            warnings.append("ground floor covers over 75% of the plot before any setbacks")
    for inf in p.reference_influences:
        if inf.reference not in known_refs:
            warnings.append(f"influence cites unknown reference {inf.reference}")

    site = {k: dim(getattr(p, k).value, *_provenance(getattr(p, k).value, getattr(p, k).source, nums, kind))
            for k, kind in (("plot_width_m", "length"), ("plot_length_m", "length"),
                            ("target_gross_area_m2", "area"))}
    return {"rooms": rooms, "adjacency": sorted([list(e) for e in edges]), "site": site,
            "warnings": warnings, "areas": summary}


# ---------- propose / approve ----------
def _context(slug: str) -> tuple[str, set[str]]:
    p = projects.project_dir(slug)
    refs = projects.read_state(slug, "reference_manifest.json")["references"]
    known = {r["id"] for r in refs}
    lines = []
    for r in refs:
        if not r.get("analysis"):
            continue
        a = json.loads((p / r["analysis"]).read_text(encoding="utf-8"))["analysis"]
        lines.append(f"- {r['id']}: {a['subject']}; style {', '.join(s['name'] for s in a['styles']) or '?'}; "
                     f"roof {a['roof_form']}; storeys {a['storeys_visible']}; materials "
                     f"{', '.join(m['name'] for m in a['materials']) or '?'}; features {', '.join(a['features'][:5])}")
    return "\n".join(lines[:12]) or "(no analysed references)", known


def propose(slug: str, requirements: str, provider=None, model: str | None = None,
            reasoning_model: str | None = None) -> dict:
    """reasoning_model: optional model id for this run, e.g. 'ollama/qwen3:8b'."""
    if len(requirements.strip()) < 15:
        raise StudioError("Requirements are too short to design from")
    cfg = config.load()
    think = None
    if provider is None:
        c = router.pick("reasoning", cfg=cfg, override=reasoning_model)
        provider, model = providers.get(c["model"].provider), c["model"].name
        reason = c["reason"]
        if c["model"].local and "thinking" in c["model"].capabilities:
            think = False  # schema-constrained output; thinking only adds minutes on a laptop GPU
    else:
        reason = "explicitly chosen"
    refs_text, known = _context(slug)
    brief = projects.read_state(slug, "design_brief.yaml")
    prompt = (f"CLIENT REQUIREMENTS:\n{requirements.strip()}\n\nREFERENCE ANALYSES:\n{refs_text}\n\n"
              f"ALREADY APPROVED IN THE BRIEF (keep unless the requirements change them):\n"
              f"{json.dumps(brief.get('preferences', {}), ensure_ascii=False)}\n\n"
              "Return the proposal as JSON matching the schema.")
    schema = ProposalLLM.model_json_schema()
    errors, result = [], None
    for _ in range(cfg["vision"]["retries"] + 1):
        raw = provider.chat(model, prompt if not errors else
                            prompt + f"\n\nYour previous answer was rejected: {errors[-1]}\nReturn valid JSON only.",
                            schema=schema, system=SYSTEM, max_tokens=4000, think=think)
        try:
            s, e = raw.find("{"), raw.rfind("}")
            result = ProposalLLM.model_validate(json.loads(raw[s:e + 1]))
            break
        except (ValueError, ValidationError) as ex:
            errors.append(str(ex)[:500])
    if result is None:
        raise StudioError(f"proposal failed validation {len(errors)} times: {errors[-1]}")
    if hasattr(provider, "unload"):
        provider.unload(model)

    checked = check(result, requirements, known)
    pdir = projects.project_dir(slug) / "proposals"
    pdir.mkdir(exist_ok=True)
    with projects.lock(slug):
        n = len(list(pdir.glob("P*.json"))) + 1
        pid = f"P{n:03d}"
        record = {"id": pid, "status": "proposed", "created": projects.now(), "model": f"{provider.name}/{model}",
                  "routing": reason, "validation_retries": len(errors), "requirements": requirements.strip(),
                  "proposal": result.model_dump(), **checked}
        (pdir / f"{pid}.json").write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        (pdir / f"{pid}.md").write_text(render(record), encoding="utf-8")
    projects.add_artifact(slug, "brief_proposal", pdir / f"{pid}.json", "design_architect", authoritative=False)
    return record


def load(slug: str, pid: str) -> dict:
    if not re.fullmatch(r"P\d{3}", pid or ""):
        raise StudioError(f"Invalid proposal id: {pid!r}")
    f = projects.project_dir(slug) / "proposals" / f"{pid}.json"
    if not f.exists():
        raise StudioError(f"Proposal not found: {pid}")
    return json.loads(f.read_text(encoding="utf-8"))


def list_proposals(slug: str) -> list[dict]:
    pdir = projects.project_dir(slug) / "proposals"
    out = []
    for f in sorted(pdir.glob("P*.json")) if pdir.exists() else []:
        r = json.loads(f.read_text(encoding="utf-8"))
        out.append({k: r[k] for k in ("id", "status", "created", "model")} |
                   {"summary": r["proposal"]["summary"], "warnings": len(r["warnings"])})
    return out


def approve(slug: str, pid: str, concept: int = 0, author: str = "user") -> dict:
    """User action: adopt a proposal and one of its concepts into design_brief.yaml."""
    rec = load(slug, pid)
    if rec["status"] != "proposed":
        raise StudioError(f"{pid} is already {rec['status']}")
    concepts = rec["proposal"]["concepts"]
    if not 0 <= concept < len(concepts):
        raise StudioError(f"{pid} has concepts 0..{len(concepts) - 1}")
    c, p = concepts[concept], rec["proposal"]
    decision = projects.add_decision(slug, f"Adopt proposal {pid}, concept '{c['name']}'",
                                     p["summary"], author, status="approved")

    def apply(b):
        b["requirements"].update(building_type=p["building_type"], floors=p["floors"],
                                 room_schedule=rec["rooms"], adjacency=rec["adjacency"])
        b["site"].update(plot_width_m=rec["site"]["plot_width_m"], plot_length_m=rec["site"]["plot_length_m"])
        b["requirements"]["target_gross_area_m2"] = rec["site"]["target_gross_area_m2"]
        b["preferences"].update(style=p["style"], concept=c["name"], massing=c["massing"],
                                roof=c["roof"], materials=c["facade_materials"])
        b["preferences"]["reference_influences"] = p["reference_influences"]
        b["assumptions"] = list(dict.fromkeys(b.get("assumptions", []) + p["assumptions"]))
        b["open_questions"] = list(dict.fromkeys(b.get("open_questions", []) + p["open_questions"]))
        b["approved_decisions"].append(decision["id"])
        b["source_proposal"] = pid

    rev = projects.update_state(slug, "design_brief.yaml", apply, author, f"approved {pid} concept {concept}")
    f = projects.project_dir(slug) / "proposals" / f"{pid}.json"
    with projects.lock(slug):
        rec.update(status="approved", approved=projects.now(), approved_concept=concept, decision=decision["id"])
        f.write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"proposal": pid, "concept": c["name"], "decision": decision["id"], "revision": rev}


def reject(slug: str, pid: str, reason: str = "", author: str = "user") -> dict:
    rec = load(slug, pid)
    if rec["status"] != "proposed":
        raise StudioError(f"{pid} is already {rec['status']}")
    f = projects.project_dir(slug) / "proposals" / f"{pid}.json"
    with projects.lock(slug):
        rec.update(status="rejected", rejected=projects.now(), reject_reason=reason)
        f.write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"proposal": pid, "status": "rejected"}


def render(rec: dict) -> str:
    p, a = rec["proposal"], rec["areas"]
    s = rec["site"]
    fmt = lambda d: f"{d['value']} [{d['provenance']}]" if d["value"] is not None else "unknown"
    out = [f"# Proposal {rec['id']} ({rec['status']})", "",
           f"Drafted by `{rec['model']}`. **A proposal, not an approved brief.** Approve with "
           f"`python -m studio_core approve <project> {rec['id']} --concept N`.", "",
           f"**{p['summary']}**", "",
           f"- Building: {p['building_type']}, {p['floors']} floor(s), style {p['style']}",
           f"- Plot: {fmt(s['plot_width_m'])} x {fmt(s['plot_length_m'])} m; target gross {fmt(s['target_gross_area_m2'])} m2",
           f"- Net room area {a['net_m2']} m2 -> gross estimate {a['gross_m2_estimate']} m2 (x{a['gross_factor']})",
           "", "## Rooms", "", "| Room | Type | Level | Area m2 | Source | Connects to |", "|---|---|---|---|---|---|"]
    for r in rec["rooms"]:
        out.append(f"| {r['name']} | {r['type']} | {r['level']} | {r['area_m2']['value']} | "
                   f"{r['area_m2']['provenance']} | {', '.join(r['adjacent_to'])} |")
    out += ["", "## Concepts", ""]
    for i, c in enumerate(p["concepts"]):
        out += [f"**{i}. {c['name']}** — {c['massing']}; roof: {c['roof']}; "
                f"facade: {', '.join(c['facade_materials'])}. Trade-offs: {c['tradeoffs']}", ""]
    if p["reference_influences"]:
        out += ["## From the references", ""] + [f"- {i['reference']}: {i['takes']}" for i in p["reference_influences"]] + [""]
    out += ["## Checks", ""] + ([f"- ⚠ {w}" for w in rec["warnings"]] or ["- no problems found"])
    out += ["", "## Assumptions", ""] + [f"- {x}" for x in p["assumptions"]]
    out += ["", "## Questions for you", ""] + [f"- {x}" for x in p["open_questions"]]
    return "\n".join(out) + "\n"
