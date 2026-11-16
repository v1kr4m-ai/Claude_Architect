"""Vision Analyst: reference image -> validated structured analysis.

The analysis is evidence, not a decision. It is stored under analysis/ and never edits
design_brief.yaml or model_spec.json; a person or the Project Director promotes findings.
"""
import io
import json
from typing import Literal

from PIL import Image, ImageOps
from pydantic import BaseModel, Field, ValidationError, field_validator

from . import config, projects, providers, router
from .projects import StudioError

Confidence = Literal["low", "medium", "high"]


class Style(BaseModel):
    name: str
    confidence: Confidence


class Material(BaseModel):
    name: str
    location: str = Field(description="where on the building, e.g. 'ground floor walls'")
    confidence: Confidence


class Dimension(BaseModel):
    what: str
    value_m: float | None = None
    provenance: Literal["estimated", "unknown"] = "unknown"
    basis: str = Field(default="", description="visible cue used for the estimate, e.g. 'door ~2.1 m'")

    @field_validator("provenance", mode="before")
    @classmethod
    def _never_measured(cls, v):
        # A photo with no known scale cannot yield a provided/measured dimension. Downgrade, don't trust.
        return v if v in ("estimated", "unknown") else "estimated"

    def model_post_init(self, _):
        if self.value_m is None:
            self.provenance = "unknown"
        elif self.provenance == "unknown":
            self.provenance = "estimated"


class ReferenceAnalysis(BaseModel):
    subject: str = Field(description="what the image shows, one sentence")
    image_type: Literal["exterior_photo", "interior_photo", "aerial", "sketch", "floor_plan",
                        "elevation_drawing", "render", "site_photo", "other"]
    building_type: str
    styles: list[Style]
    storeys_visible: int | None = None
    massing: str = Field(description="overall form and volumes")
    roof_form: str
    materials: list[Material]
    openings: str = Field(description="windows and doors: type, proportion, rhythm")
    features: list[str] = Field(description="notable architectural features actually visible")
    landscape: str
    camera_view: str
    spatial_relationships: list[str] = Field(default_factory=list)
    dimensions: list[Dimension] = Field(default_factory=list)
    uncertainties: list[str] = Field(description="what cannot be determined from this image")
    overall_confidence: Confidence


SYSTEM = ("You are an architectural vision analyst. Describe only what is visible in the image. "
          "Do not invent rooms, dimensions or features you cannot see. Any size you give is an "
          "estimate from a visible cue and must say which cue. List what you cannot determine.")
# Step 1 (vision model, free text). Small local vision models describe far better in prose than
# when forced straight into a large JSON schema (measured, see MODEL_INVENTORY.md), so the schema
# is filled in a second step by a text model.
DESCRIBE = ("Describe this architectural image for an architect. Cover, in order: what it shows and whether "
            "it is a photo, render, sketch or plan; building type; architectural style; number of storeys "
            "visible; overall form and volumes; roof form; windows and doors (count per floor per visible "
            "face, proportions, rhythm); materials and colours and where they are; notable features; "
            "landscape and site; camera position; any size in metres you can estimate and the visible cue "
            "you used; and what cannot be determined from this view. Describe only what is visible.")
# Step 2 (text model, schema-constrained).
STRUCTURE = ("Convert the description of an architectural image below into the JSON schema.\n"
             "Rules: use only facts stated in the description. storeys_visible is the integer storey count "
             "if stated. styles are architectural style names only (modern, colonial, ...). materials are "
             "real materials or finishes with their colour. dimensions: include a size only if the "
             "description gives it in metres with a visible cue; sizes in other or unnamed units go under "
             "uncertainties instead. Where the description is silent write 'not stated'.\n\nDESCRIPTION:\n")


def _prepare(path, max_px: int) -> bytes:
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((max_px, max_px))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90)
        return buf.getvalue()


def parse(raw: str) -> ReferenceAnalysis:
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON object in model output")
    return ReferenceAnalysis.model_validate(json.loads(raw[start:end + 1]))


def analyse(slug: str, ref_id: str, provider=None, model: str | None = None, unload: bool = True,
            vision_model: str | None = None) -> dict:
    """vision_model: optional model id for this run, e.g. 'ollama/llava:latest'."""
    cfg = config.load()
    p = projects.project_dir(slug)
    ref = next((r for r in projects.read_state(slug, "reference_manifest.json")["references"]
                if r["id"] == ref_id), None)
    if not ref:
        raise StudioError(f"Unknown reference: {ref_id}")
    if ref["kind"] in ("video", "pdf"):
        raise StudioError(f"{ref_id} is a {ref['kind']}; analyse its frames/pages ({ref_id}-01 ...)")
    if provider is None:
        v, j = router.pick("vision", cfg=cfg, override=vision_model), router.pick("json", cfg=cfg)
        provider, model = providers.get(v["model"].provider), v["model"].name
        jprovider, jmodel = providers.get(j["model"].provider), j["model"].name
        reason = {"vision": v["reason"], "json": j["reason"]}
    else:
        jprovider, jmodel, reason = provider, model, "explicitly chosen"
    image = _prepare(p / ref["original"], cfg["vision"]["max_image_px"])

    description = provider.chat(model, DESCRIBE, images=[image], system=SYSTEM).strip()
    if not description:
        raise StudioError(f"{ref_id}: vision model returned nothing")
    if unload and hasattr(provider, "unload"):
        provider.unload(model)  # one model in VRAM at a time on an 8 GB card

    schema = ReferenceAnalysis.model_json_schema()
    prompt, errors, result = STRUCTURE + description, [], None
    for _ in range(cfg["vision"]["retries"] + 1):
        raw = jprovider.chat(jmodel, prompt, schema=schema)
        try:
            result = parse(raw)
            break
        except (ValueError, ValidationError) as e:
            errors.append(str(e)[:500])
            prompt = (STRUCTURE + description +
                      f"\n\nYour previous answer was rejected: {errors[-1]}\nReturn valid JSON only.")
    if result is None:
        raise StudioError(f"{ref_id}: model output failed validation {len(errors)} times: {errors[-1]}")
    if unload and hasattr(jprovider, "unload"):
        jprovider.unload(jmodel)  # free VRAM for whatever runs next (ComfyUI, Blender)

    record = {"reference": ref_id, "model": f"{provider.name}/{model}",
              "structured_by": f"{jprovider.name}/{jmodel}", "routing": reason,
              "analysed": projects.now(), "validation_retries": len(errors), "status": "unreviewed",
              "description": description, "analysis": result.model_dump()}
    out = p / "analysis" / f"{ref_id}.json"
    out.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

    def mark(d):
        next(r for r in d["references"] if r["id"] == ref_id)["analysis"] = out.relative_to(p).as_posix()

    projects.update_state(slug, "reference_manifest.json", mark, "vision_analyst", f"analysed {ref_id}")
    projects.add_artifact(slug, "reference_analysis", out, "vision_analyst", authoritative=False)
    write_report(slug)
    return record


def analyse_all(slug: str, force: bool = False, vision_model: str | None = None) -> dict:
    refs = [r for r in projects.read_state(slug, "reference_manifest.json")["references"]
            if r["kind"] in ("image", "frame", "page") and (force or not r.get("analysis"))
            and not r.get("near_duplicate_of")]
    done, failed = [], []
    for r in refs:
        try:
            done.append(analyse(slug, r["id"], vision_model=vision_model)["reference"])
        except (StudioError, providers.ProviderError) as e:
            failed.append({"reference": r["id"], "error": str(e)})
    return {"analysed": done, "failed": failed}


def write_report(slug: str) -> str:
    p = projects.project_dir(slug)
    lines = [f"# Reference analysis: {projects.read_state(slug, 'project.json')['name']}", "",
             "Machine analysis of reference images. **Unreviewed evidence, not approved design "
             "decisions.** Every size below is an estimate from a photo with no known scale.", ""]
    for f in sorted((p / "analysis").glob("REF*.json")):
        rec = json.loads(f.read_text(encoding="utf-8"))
        a = rec["analysis"]
        lines += [f"## {rec['reference']}: {a['subject']}", "",
                  f"- Seen by `{rec['model']}`, structured by `{rec.get('structured_by', rec['model'])}` | "
                  f"overall confidence: **{a['overall_confidence']}**",
                  f"- Type: {a['image_type']} | building: {a['building_type']} | "
                  f"storeys visible: {a['storeys_visible'] if a['storeys_visible'] is not None else 'unknown'}",
                  "- Styles: " + (", ".join(f"{s['name']} ({s['confidence']})" for s in a["styles"]) or "none"),
                  f"- Massing: {a['massing']}", f"- Roof: {a['roof_form']}", f"- Openings: {a['openings']}",
                  "- Materials: " + ("; ".join(f"{m['name']} @ {m['location']} ({m['confidence']})"
                                               for m in a["materials"]) or "none"),
                  "- Features: " + ("; ".join(a["features"]) or "none"),
                  f"- Landscape: {a['landscape']}", f"- Camera: {a['camera_view']}"]
        if a["spatial_relationships"]:
            lines.append("- Spatial: " + "; ".join(a["spatial_relationships"]))
        for d in a["dimensions"]:
            v = f"~{d['value_m']} m" if d["value_m"] is not None else "unknown"
            lines.append(f"- Dimension [{d['provenance']}]: {d['what']} = {v}" +
                         (f" (basis: {d['basis']})" if d["basis"] else ""))
        lines += ["- Cannot determine: " + ("; ".join(a["uncertainties"]) or "nothing listed"), ""]
        if rec.get("description"):
            lines += ["<details><summary>Vision model's own description</summary>", "",
                      rec["description"], "", "</details>", ""]
    out = p / "analysis" / "reference_analysis.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out.as_posix()
