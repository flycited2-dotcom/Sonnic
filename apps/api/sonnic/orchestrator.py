"""Local orchestration for explicit demo or real modes."""
import asyncio
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Protocol

from .models import DreamInput, FreeResult, Symbol
from .store import Store
from .real_provider import ProviderError

CONTENT = Path(__file__).resolve().parents[3] / "packages" / "demo_content"


class Provider(Protocol):
    async def extract(self, body: DreamInput) -> list[Symbol]: ...
    async def free(self, body: DreamInput, symbols: list[Symbol]) -> FreeResult: ...


class DemoProvider:
    async def extract(self, body: DreamInput) -> list[Symbol]:
        # Literal word matching only. No hidden meanings, guesses or risk diagnosis.
        rules = [
            (r"\b(?:дом(?:а|е|ом)?|house|home)\b", "Дом", "Home"),
            (r"\b(?:вод(?:а|ы|е|у|ой)|water)\b", "Вода", "Water"),
            (r"\b(?:ключ(?:а|и|ом)?|keys?)\b", "Ключ", "Key"),
            (r"\b(?:двер(?:ь|и|ью)|doors?)\b", "Дверь", "Door"),
            (r"\b(?:лес(?:а|у|ом)?|forest)\b", "Лес", "Forest"),
            (r"\b(?:мор(?:е|я|ем)|sea|ocean)\b", "Море", "Sea"),
        ]
        symbols = []
        for pattern, ru, en in rules:
            match = re.search(pattern, body.dream_text, re.IGNORECASE)
            if match:
                symbols.append(Symbol(label=ru if body.locale == "ru-RU" else en, evidence=match.group(0)))
        return symbols

    async def free(self, body: DreamInput, symbols: list[Symbol]) -> FreeResult:
        data = json.loads((CONTENT / f"{body.locale}.json").read_text(encoding="utf-8"))
        return FreeResult(**data, symbols=symbols, locale=body.locale,
                          emotional_tone=body.wake_emotion or ("Не указано" if body.locale == "ru-RU" else "Not specified"))


class Orchestrator:
    def __init__(self, store: Store, provider: Provider | None = None):
        self.store, self.provider = store, provider or DemoProvider()
        self.semaphore = asyncio.Semaphore(2)
        self.tasks: dict[str, asyncio.Task] = {}

    def enqueue(self, did: str, body: DreamInput):
        task = asyncio.create_task(self.run(did, body))
        self.tasks[did] = task
        task.add_done_callback(lambda _: self.tasks.pop(did, None))

    async def run(self, did: str, body: DreamInput):
        started = time.monotonic()
        provider = self.provider.for_job() if hasattr(self.provider, "for_job") else self.provider
        try:
            async with asyncio.timeout(100):
                async with self.semaphore:
                    self.store.stage(did, "extracting")
                    symbols = await provider.extract(body)
                    self.store.stage(did, "composing")
                    result = await provider.free(body, symbols)
                    result = FreeResult.model_validate(result)
                    template = (CONTENT / f"{body.locale}.json").read_bytes()
                    telemetry = provider.telemetry() if hasattr(provider, "telemetry") else {
                        "provider": "demo", "model": None, "template_version": "demo-v1",
                        "template_checksum": hashlib.sha256(template).hexdigest(),
                        "latency_ms": round((time.monotonic()-started)*1000),
                        "tokens": 0, "cost_estimate": 0, "research_retrieval_used": False,
                    }
                    telemetry["latency_ms"] = round((time.monotonic()-started)*1000)
                    self.store.finish(did, result.model_dump_json(), telemetry)
        except ProviderError as error:
            self.store.fail(did, error.code)
        except TimeoutError:
            self.store.fail(did, "timeout")
        except asyncio.CancelledError:
            self.store.fail(did, "interrupted")
            raise
        except Exception:
            # Do not include exception text: provider errors can contain private input.
            self.store.fail(did, "processing_failed")
        finally:
            if hasattr(provider, "telemetry"):
                telemetry = provider.telemetry()
                telemetry["latency_ms"] = round((time.monotonic()-started)*1000)
                self.store.telemetry(did, telemetry)

    async def cancel(self, did: str):
        task = self.tasks.get(did)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def shutdown(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
