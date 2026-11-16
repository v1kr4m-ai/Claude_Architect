"""Model providers behind one small interface.

A provider reports the models it really has and what each can really do; nothing is assumed.
Adding a provider = one class with name / available() / models() / chat(), then list it in all().
"""
import base64
import json
import os
from dataclasses import asdict, dataclass, field

import httpx

from . import config


class ProviderError(Exception):
    pass


SIZE_CACHE = config.ROOT / ".cache" / "model_loaded_mb.json"


def _load_sizes() -> dict:
    try:
        return json.loads(SIZE_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@dataclass
class ModelInfo:
    provider: str
    name: str
    capabilities: set = field(default_factory=set)  # completion vision tools thinking embedding json
    local: bool = True
    size_mb: int = 0          # weights on disk; a proxy for the VRAM it needs
    context: int = 0

    @property
    def id(self) -> str:
        return f"{self.provider}/{self.name}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["capabilities"] = sorted(self.capabilities)
        d["id"] = self.id
        return d


class Ollama:
    name = "ollama"

    def __init__(self, cfg=None):
        c = (cfg or config.load())["ollama"]
        self.base, self.timeout, self.num_ctx = c["base_url"].rstrip("/"), c["timeout_seconds"], c["num_ctx"]
        self.num_predict = c["num_predict"]  # caps runaway repetition loops (seen on llama3.2-vision)

    def available(self) -> bool:
        try:
            return httpx.get(f"{self.base}/api/version", timeout=3).status_code == 200
        except httpx.HTTPError:
            return False

    def models(self) -> list[ModelInfo]:
        out = []
        for m in httpx.get(f"{self.base}/api/tags", timeout=10).json().get("models", []):
            show = httpx.post(f"{self.base}/api/show", json={"model": m["name"]}, timeout=20).json()
            caps = set(show.get("capabilities") or [])
            if "completion" in caps:
                caps.add("json")  # Ollama constrains output to a JSON schema for any chat model
            ctx = next((v for k, v in (show.get("model_info") or {}).items()
                        if k.endswith(".context_length")), 0)
            loaded = _load_sizes().get(m["name"], 0)
            out.append(ModelInfo("ollama", m["name"], caps, local=not m.get("remote_host"),
                                 # real loaded footprint once seen; file size until then
                                 size_mb=max(m.get("size", 0) // 2**20, loaded), context=ctx))
        return out

    def chat(self, model: str, prompt: str, images: list[bytes] = (), schema: dict | None = None,
             system: str = "", unload: bool = False, max_tokens: int | None = None,
             think: bool | None = None) -> str:
        msgs = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt,
             **({"images": [base64.b64encode(i).decode() for i in images]} if images else {})}]
        body = {"model": model, "messages": msgs, "stream": False,
                # num_ctx capped: Ollama's 32k default spills an 8 GB card
                "options": {"temperature": 0.2, "num_ctx": self.num_ctx,
                            "num_predict": max_tokens or self.num_predict},
                # keep_alive 0 frees VRAM as soon as the answer is back (8 GB card, one model at a time)
                "keep_alive": 0 if unload else "5m"}
        if schema:
            body["format"] = schema
        if think is not None:  # only for models reporting "thinking"; others reject the field
            body["think"] = think
        try:
            r = httpx.post(f"{self.base}/api/chat", json=body, timeout=self.timeout)
        except httpx.HTTPError as e:  # timeout / refused: one failed item, not a crashed batch
            raise ProviderError(f"ollama {model}: {type(e).__name__} after {self.timeout}s limit") from e
        if r.status_code != 200:
            raise ProviderError(f"ollama {model}: HTTP {r.status_code} {r.text[:300]}")
        self._record_size(model)
        return r.json()["message"]["content"]

    def _record_size(self, model: str) -> None:
        try:
            for m in httpx.get(f"{self.base}/api/ps", timeout=5).json().get("models", []):
                if m["name"] == model:
                    sizes = _load_sizes()
                    sizes[model] = m["size"] // 2**20
                    SIZE_CACHE.parent.mkdir(exist_ok=True)
                    SIZE_CACHE.write_text(json.dumps(sizes, indent=1), encoding="utf-8")
        except (httpx.HTTPError, OSError, ValueError, KeyError):
            pass  # cache is an optimisation; never fail a chat over it

    def unload(self, model: str) -> None:
        httpx.post(f"{self.base}/api/generate", json={"model": model, "keep_alive": 0}, timeout=30)

    def loaded(self) -> list[str]:
        return [m["name"] for m in httpx.get(f"{self.base}/api/ps", timeout=5).json().get("models", [])]


class Anthropic:
    """Cloud provider. Only reachable when privacy.allow_cloud is on AND a credential exists.
    NOT live-tested on this machine (no credential present at build time)."""
    name = "anthropic"
    _MODELS = {"claude-opus-5-5": 1_000_000, "claude-haiku-5-5": 1_000_000}

    def available(self) -> bool:
        return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))

    def models(self) -> list[ModelInfo]:
        return [ModelInfo("anthropic", n, {"completion", "vision", "tools", "thinking", "json"},
                          local=False, context=c) for n, c in self._MODELS.items()]

    def chat(self, model: str, prompt: str, images: list[bytes] = (), schema: dict | None = None,
             system: str = "", unload: bool = False, max_tokens: int | None = None,
             think: bool | None = None) -> str:
        import anthropic
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                "data": base64.b64encode(i).decode()}} for i in images]
        if schema:  # schema is enforced by the caller's validator, same as every other provider
            prompt += "\n\nReply with one JSON object only, matching this JSON Schema:\n" + json.dumps(schema)
        content.append({"type": "text", "text": prompt})
        try:
            r = anthropic.Anthropic().messages.create(
                model=model, max_tokens=max_tokens or 16000, messages=[{"role": "user", "content": content}],
                **({"system": system} if system else {}))
        except anthropic.APIError as e:
            raise ProviderError(f"anthropic {model}: {e}") from e
        if r.stop_reason == "refusal":
            raise ProviderError(f"anthropic {model}: request refused")
        return next(b.text for b in r.content if b.type == "text")


def all_providers() -> list:
    return [Ollama(), Anthropic()]


def inventory() -> list[ModelInfo]:
    """Every model from every provider that is reachable right now."""
    out = []
    for p in all_providers():
        if p.available():
            out.extend(p.models())
    return out


def get(name: str):
    for p in all_providers():
        if p.name == name:
            return p
    raise ProviderError(f"Unknown provider: {name}")
