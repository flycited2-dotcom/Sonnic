"""Explicitly enabled OpenAI Responses adapter; no network at construction time."""
import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Literal

import httpx
from pydantic import Field, ValidationError

from .models import DreamInput, FreeResult, Model, Symbol

ROOT = Path(__file__).resolve().parents[3]
ENDPOINT = "https://api.openai.com/v1/responses"


class ProviderError(Exception):
    """Only fixed application codes may cross the provider boundary."""
    CODES = {"provider_auth", "provider_rate_limited", "provider_unavailable", "provider_refused",
             "invalid_model_output", "safety_support", "safety_refused", "timeout"}

    def __init__(self, code: str):
        self.code = code if code in self.CODES else "provider_unavailable"
        super().__init__(self.code)


@dataclass(frozen=True)
class AISettings:
    api_key: str = field(repr=False)
    extract_model: str
    free_model: str

    @classmethod
    def from_env(cls):
        names = ("OPENAI_API_KEY", "OPENAI_EXTRACT_MODEL", "OPENAI_FREE_MODEL")
        if os.getenv("AI_ALLOW_PAID_REQUESTS") != "true":
            raise RuntimeError("Real mode requires AI_ALLOW_PAID_REQUESTS=true")
        if any(not os.getenv(name, "").strip() for name in names):
            raise RuntimeError("Real mode requires OPENAI_API_KEY, OPENAI_EXTRACT_MODEL and OPENAI_FREE_MODEL")
        return cls(*(os.environ[name].strip() for name in names))


class Extraction(Model):
    symbols: list[Symbol] = Field(max_length=6)
    safety_action: Literal["allow", "support", "refuse"]


class Reflection(Model):
    title: str = Field(min_length=1, max_length=150)
    summary: str = Field(min_length=20, max_length=4000)
    key_connection: str = Field(min_length=10, max_length=1000)
    reflection_question: str = Field(min_length=10, max_length=600)


class Review(Model):
    approved: bool


def response_schema(model: type[Model]) -> dict:
    schema = model.model_json_schema()
    # These string limits are checked locally; use the conservative API subset.
    def clean(node):
        if isinstance(node, dict):
            for key in ("minLength", "maxLength", "default"):
                node.pop(key, None)
            for child in node.values():
                clean(child)
        elif isinstance(node, list):
            for child in node:
                clean(child)
    clean(schema)
    return schema


class OpenAIProvider:
    def __init__(self, settings: AISettings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings, self.transport = settings, transport
        raw = (ROOT / "packages/prompt_registry/real-v1.json").read_bytes()
        self.prompts = json.loads(raw)
        self.checksum = hashlib.sha256(raw).hexdigest()
        self.calls: list[dict] = []

    def for_job(self):
        # Isolated telemetry for concurrent dreams and retries; never global text caches.
        return OpenAIProvider(self.settings, self.transport)

    def telemetry(self):
        return {"provider": "openai", "prompt_version": self.prompts["version"],
                "prompt_checksum": self.checksum, "schema_version": "real-v1",
                "calls": self.calls, "cost_estimate": None, "research_retrieval_used": False}

    async def request(self, stage: str, model: str, shape: type[Model], data: dict, max_tokens: int):
        stats = {"stage": stage, "model": model, "input_tokens": None, "output_tokens": None, "status": "failed"}
        self.calls.append(stats)
        started = time.monotonic()
        payload = {"model": model, "store": False, "max_output_tokens": max_tokens,
                   "instructions": self.prompts["policy"] + "\n" + self.prompts[stage],
                   "input": [{"role": "user", "content": json.dumps(data, ensure_ascii=False)}],
                   "text": {"format": {"type": "json_schema", "name": stage + "_v1", "strict": True,
                                       "schema": response_schema(shape)}}}
        try:
            # Fixed official endpoint, no redirects/proxies, no automatic paid retries.
            async with httpx.AsyncClient(timeout=30, trust_env=False, transport=self.transport) as client:
                async with asyncio.timeout(30):
                    response = await client.post(ENDPOINT, headers={"Authorization": "Bearer " + self.settings.api_key}, json=payload)
            if response.status_code in (401, 403):
                raise ProviderError("provider_auth")
            if response.status_code == 429:
                raise ProviderError("provider_rate_limited")
            if not response.is_success:
                raise ProviderError("provider_unavailable")
            envelope = response.json()
            usage = envelope.get("usage") or {}
            for key in ("input_tokens", "output_tokens"):
                value = usage.get(key)
                if type(value) is int and value >= 0:
                    stats[key] = value
            if envelope.get("status") != "completed":
                raise ProviderError("invalid_model_output")
            parts = [part for item in envelope.get("output", []) if item.get("type") == "message"
                     for part in item.get("content", [])]
            if any(part.get("type") == "refusal" for part in parts):
                raise ProviderError("provider_refused")
            texts = [part["text"] for part in parts if part.get("type") == "output_text"]
            if len(texts) != 1:
                raise ProviderError("invalid_model_output")
            result = shape.model_validate_json(texts[0], strict=True)
            stats["status"] = "completed"
            return result
        except (httpx.TimeoutException, TimeoutError):
            raise ProviderError("timeout") from None
        except httpx.HTTPError:
            raise ProviderError("provider_unavailable") from None
        except (ValidationError, ValueError, TypeError, KeyError, AttributeError):
            raise ProviderError("invalid_model_output") from None
        finally:
            stats["latency_ms"] = round((time.monotonic() - started) * 1000)

    async def extract(self, body: DreamInput) -> list[Symbol]:
        result = await self.request("extract", self.settings.extract_model, Extraction, body.model_dump(mode="json"), 2200)
        if result.safety_action != "allow":
            raise ProviderError("safety_support" if result.safety_action == "support" else "safety_refused")
        # Quotes must be present verbatim, even if the model claims schema compliance.
        if any(symbol.evidence not in body.dream_text for symbol in result.symbols):
            raise ProviderError("invalid_model_output")
        return result.symbols

    async def free(self, body: DreamInput, symbols: list[Symbol]) -> FreeResult:
        data = {"dream": body.model_dump(mode="json"), "symbols": [s.model_dump() for s in symbols]}
        draft = await self.request("free", self.settings.free_model, Reflection, data, 3000)
        review = await self.request("review", self.settings.extract_model, Review,
                                    {**data, "candidate": draft.model_dump()}, 1200)
        if not review.approved:
            raise ProviderError("invalid_model_output")
        content = json.loads((ROOT / f"packages/demo_content/{body.locale}.json").read_text(encoding="utf-8"))
        return FreeResult(**draft.model_dump(), symbols=symbols,
                          emotional_tone=body.wake_emotion or ("Не указано" if body.locale == "ru-RU" else "Not specified"),
                          locked_sections=content["locked_sections"], locale=body.locale,
                          mode="real", evidence_basis="model_from_user_text", research_retrieval_used=False)
