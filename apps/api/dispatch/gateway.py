"""Model gateway. Agents never import an SDK; they call `complete()` with a schema and get a validated object.

Providers, chosen per request by the caller (the UI keeps the key in the visitor's browser and sends it
as headers; the server stores nothing):
  - anthropic: the official Anthropic SDK, adaptive thinking, structured outputs, prompt caching on the stable prefix
  - openai:    any OpenAI-compatible chat-completions endpoint (OpenAI, vLLM, Ollama, ...) with JSON-schema output
  - mock:      deterministic outputs derived from the input (see mock.py), so the demo runs without a key
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from .store import Store

T = TypeVar("T", bound=BaseModel)

# USD per million tokens (input, output, cache read), first-party API rates
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
    "chair": ("claude-opus-5", "high"),
    "researcher": ("claude-sonnet-5", "medium"),
    "scoper": ("claude-sonnet-5", "medium"),
    "estimator": ("claude-sonnet-5", "medium"),
    "sub_estimator": ("claude-sonnet-5", "low"),
    "pricer": ("claude-opus-5", "medium"),
    "risk": ("claude-opus-5", "medium"),
    "writer": ("claude-sonnet-5", "medium"),
    "critic": ("claude-opus-5", "high"),
    "arbiter": ("claude-opus-5", "high"),
    "defence": ("claude-sonnet-5", "medium"),
}

ANTHROPIC_MODELS = ["auto", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"]


@dataclass
class Settings:
    provider: str = "mock"  # mock | anthropic | openai
    model: str = "auto"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        if os.environ.get("ANTHROPIC_API_KEY"):
            return cls(provider="anthropic", model="auto", api_key=os.environ["ANTHROPIC_API_KEY"])
        if os.environ.get("OPENAI_API_KEY"):
            return cls(provider="openai", model=os.environ.get("OPENAI_MODEL", "gpt-4o"), base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"), api_key=os.environ["OPENAI_API_KEY"])
        return cls()

    @classmethod
    def from_headers(cls, headers: Any) -> "Settings":
        """X-Model-Provider / X-Model-Name / X-Model-Base-Url / X-Model-Key. Falls back to the environment, then mock."""
        provider = (headers.get("x-model-provider") or "").strip().lower()
        if provider not in ("anthropic", "openai", "mock"):
            return cls.from_env()
        s = cls(provider=provider)
        s.model = (headers.get("x-model-name") or ("auto" if provider == "anthropic" else "gpt-4o")).strip()
        if provider == "anthropic" and s.model not in ANTHROPIC_MODELS:
            s.model = "auto"
        s.base_url = (headers.get("x-model-base-url") or "https://api.openai.com/v1").strip()
        s.api_key = (headers.get("x-model-key") or "").strip()
        if provider != "mock" and not s.api_key:
            env = cls.from_env()
            if env.provider == provider:
                s.api_key = env.api_key
        return s

    @property
    def label(self) -> str:
        if self.provider == "anthropic":
            return "Anthropic · " + ("Opus 5 for judgment, Sonnet 5 for volume" if self.model == "auto" else self.model)
        if self.provider == "openai":
            return f"OpenAI-compatible · {self.model} @ {self.base_url}"
        return "Mock · deterministic, no key"


class Gateway:
    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings
        self.current: dict[str, str] = {"meeting": "", "turn": ""}
        self._anthropic = None

    async def check(self) -> dict[str, Any]:
        """A one-line health check the settings panel can run before a demo."""

        class Ping(BaseModel):
            ok: bool
            model_name: str

        if self.settings.provider == "mock":
            return {"ok": True, "detail": "Mock provider: deterministic answers, no key needed. Pick Anthropic or an OpenAI-compatible endpoint to use a real model."}
        try:
            out = await self.complete(role="dispatcher", schema=Ping, system="Reply with ok=true and the name you are known as.", user="ping", case_id="settings-check")
            return {"ok": True, "detail": f"{self.settings.label} answered: {out.model_name}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": str(e)[:300]}

    def _client(self):
        import anthropic

        if self._anthropic is None:
            self._anthropic = anthropic.AsyncAnthropic(api_key=self.settings.api_key or None)
        return self._anthropic

    def _record(self, case_id: str, role: str, model: str, tin: int, tout: int, cached: int, cost: float, t0: float) -> None:
        ms = int((time.time() - t0) * 1000)
        rec = getattr(self.store, "record_call", None)
        if rec is None:
            return
        try:
            if self.current.get("turn"):
                rec(self.current["meeting"], self.current["turn"], role, model, tin, tout, cached, cost, ms)  # roundtable signature
            else:
                rec(case_id, role, model, tin, tout, cached, cost, ms)  # dispatch signature
        except TypeError:
            rec(case_id, role, model, tin, tout, cached, cost, ms)

    async def complete(self, *, role: str, schema: type[T], system: str, user: str, case_id: str, context: dict[str, Any] | None = None) -> T:
        t0 = time.time()
        s = self.settings
        if s.provider == "mock":
            from . import mock

            out = mock.respond(role, schema, context or {})
            self._record(case_id, role, "mock", 0, 0, 0, 0.0, t0)
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
        self._record(case_id, role, model, u.input_tokens, u.output_tokens, cached, cost, t0)
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
        text = data["choices"][0]["message"]["content"].strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text[text.find("{") :]
        usage = data.get("usage", {})
        self._record(case_id, role, model, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), 0, 0.0, t0)
        return schema.model_validate_json(text[text.find("{") : text.rfind("}") + 1])
