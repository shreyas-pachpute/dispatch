"""Model gateway. Agents never import an SDK; they call `complete()` with a schema and get a validated object.

Providers, chosen at runtime from the control room's settings panel (the key lives in process memory only):
  - anthropic: the official Anthropic SDK, adaptive thinking, structured outputs, prompt caching on the stable prefix
  - openai:    any OpenAI-compatible chat-completions endpoint (OpenAI, vLLM, Ollama, ...) with JSON-schema output
  - mock:      deterministic outputs derived from the input (see mock.py), so the demo and the tests run without a key
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from .store import Store

T = TypeVar("T", bound=BaseModel)

# USD per million tokens (input, output, cache read), first-party API rates; other providers are reported as 0 unless set
PRICES = {
    "claude-opus-5": (5.0, 25.0, 0.5),
    "claude-sonnet-5": (2.0, 10.0, 0.2),
    "claude-haiku-4-5": (1.0, 5.0, 0.1),
}

# "auto" on Anthropic: judgment roles on Opus 5, volume roles on Sonnet 5
ROLE_MODEL = {
    "dispatcher": ("claude-opus-5", "medium"),
    "intake": ("claude-sonnet-5", "medium"),
    "accounts": ("claude-opus-5", "medium"),
    "customer": ("claude-opus-5", "high"),
    "followup": ("claude-sonnet-5", "low"),
    "analyst": ("claude-opus-5", "medium"),
    "analyst_answer": ("claude-sonnet-5", "low"),
    "reviewer": ("claude-opus-5", "high"),
}

ANTHROPIC_MODELS = ["auto", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"]


@dataclass
class Settings:
    provider: str = "mock"  # mock | anthropic | openai
    model: str = "auto"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        if os.environ.get("ANTHROPIC_API_KEY"):
            return cls(provider="anthropic", model="auto", api_key=os.environ["ANTHROPIC_API_KEY"])
        if os.environ.get("OPENAI_API_KEY"):
            return cls(provider="openai", model=os.environ.get("OPENAI_MODEL", "gpt-4o"), base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"), api_key=os.environ["OPENAI_API_KEY"])
        return cls()

    def public(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "has_key": bool(self.api_key),
            "key_hint": (self.api_key[:6] + "…" + self.api_key[-3:]) if len(self.api_key) > 12 else ("set" if self.api_key else ""),
            "anthropic_models": ANTHROPIC_MODELS,
        }

    @property
    def label(self) -> str:
        if self.provider == "anthropic":
            return "Anthropic · " + ("Opus 5 for judgment, Sonnet 5 for volume" if self.model == "auto" else self.model)
        if self.provider == "openai":
            return f"OpenAI-compatible · {self.model} @ {self.base_url}"
        return "Mock · deterministic, no key"


class Gateway:
    def __init__(self, store: Store, settings: Settings | None = None):
        self.store = store
        self.settings = settings or Settings.from_env()
        self._anthropic = None
        self._anthropic_key = None

    def configure(self, **kw: Any) -> Settings:
        for k, v in kw.items():
            if v is not None and hasattr(self.settings, k):
                setattr(self.settings, k, v)
        if self.settings.provider == "anthropic" and self.settings.model not in ANTHROPIC_MODELS:
            self.settings.model = "auto"
        return self.settings

    async def check(self) -> dict[str, Any]:
        """A one-line health check the settings panel can run before a demo."""
        from pydantic import BaseModel as _B

        class Ping(_B):
            ok: bool
            model_name: str

        try:
            out = await self.complete(role="dispatcher", schema=Ping, system="Reply with ok=true and the name you are known as.", user="ping", case_id="settings-check")
            return {"ok": True, "detail": f"{self.settings.label} answered: {out.model_name}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": str(e)[:300]}

    def _client(self):
        import anthropic

        if self._anthropic is None or self._anthropic_key != self.settings.api_key:
            self._anthropic = anthropic.AsyncAnthropic(api_key=self.settings.api_key or None)
            self._anthropic_key = self.settings.api_key
        return self._anthropic

    async def complete(self, *, role: str, schema: type[T], system: str, user: str, case_id: str, context: dict[str, Any] | None = None) -> T:
        t0 = time.time()
        s = self.settings
        if s.provider == "mock":
            from . import mock

            out = mock.respond(role, schema, context or {})
            self.store.record_call(case_id, role, "mock", 0, 0, 0, 0.0, int((time.time() - t0) * 1000))
            return out
        if s.provider == "anthropic":
            return await self._anthropic_complete(role, schema, system, user, case_id, t0)
        return await self._openai_complete(role, schema, system, user, case_id, t0)

    async def _anthropic_complete(self, role: str, schema: type[T], system: str, user: str, case_id: str, t0: float) -> T:
        s = self.settings
        model, effort = ROLE_MODEL.get(role, ("claude-opus-5", "medium"))
        if s.model != "auto":
            model = s.model
        kwargs: dict[str, Any] = {}
        if not model.startswith("claude-haiku"):
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"effort": effort}
        response = await self._client().messages.parse(
            model=model,
            max_tokens=8000,
            # the stable prefix (role prompt) is cached; the volatile pack comes in the user turn
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_format=schema,
            **kwargs,
        )
        u = response.usage
        p_in, p_out, p_cache = PRICES.get(model, (5.0, 25.0, 0.5))
        cached = getattr(u, "cache_read_input_tokens", 0) or 0
        cost = (u.input_tokens * p_in + u.output_tokens * p_out + cached * p_cache) / 1e6
        self.store.record_call(case_id, role, model, u.input_tokens, u.output_tokens, cached, cost, int((time.time() - t0) * 1000))
        if response.stop_reason == "refusal":
            raise RuntimeError(f"model refused: {getattr(response, 'stop_details', None)}")
        if response.parsed_output is None:
            raise RuntimeError("model returned no parsable output")
        return response.parsed_output

    async def _openai_complete(self, role: str, schema: type[T], system: str, user: str, case_id: str, t0: float) -> T:
        s = self.settings
        model = s.model if s.model and s.model != "auto" else "gpt-4o"
        json_schema = schema.model_json_schema()
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system + "\n\nRespond with a single JSON object matching the required schema and nothing else."},
                {"role": "user", "content": user + "\n\nJSON schema:\n" + json.dumps(json_schema)},
            ],
            "response_format": {"type": "json_schema", "json_schema": {"name": schema.__name__, "schema": json_schema}},
            "temperature": 0.2,
        }
        headers = {"Authorization": f"Bearer {s.api_key}"} if s.api_key else {}
        async with httpx.AsyncClient(timeout=180) as http:
            r = await http.post(s.base_url.rstrip("/") + "/chat/completions", json=body, headers=headers)
            if r.status_code == 400 and "response_format" in r.text:
                body.pop("response_format")
                r = await http.post(s.base_url.rstrip("/") + "/chat/completions", json=body, headers=headers)
            r.raise_for_status()
            data = r.json()
        text = data["choices"][0]["message"]["content"]
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text[text.find("{") :]
        usage = data.get("usage", {})
        self.store.record_call(case_id, role, model, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), 0, 0.0, int((time.time() - t0) * 1000))
        return schema.model_validate_json(text[text.find("{") : text.rfind("}") + 1])
