"""Offline by default. --live explicitly sends only checked-in synthetic dreams."""
import argparse
import asyncio
from difflib import SequenceMatcher
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
from apps.api.sonnic.models import DreamInput
from apps.api.sonnic.real_provider import AISettings, OpenAIProvider, ProviderError


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Authorize paid API calls for the synthetic quality set")
    args = parser.parse_args()
    cases = json.loads((ROOT / "tests/quality/dreams.json").read_text(encoding="utf-8"))
    for case in cases:
        DreamInput(dream_text=case["dream_text"], locale=case["locale"])
    if not args.live:
        print(f"{len(cases)} valid synthetic cases; live quality NOT evaluated. No network calls.")
        return
    load_dotenv(ROOT / ".env")
    if os.getenv("AI_MODE") != "real":
        raise SystemExit("Live evaluation requires AI_MODE=real and explicit API settings.")
    settings = AISettings.from_env()
    report = {"status": "manual_review_required", "cases": [], "similar_pairs": []}
    for case in cases:
        provider = OpenAIProvider(settings)
        body = DreamInput(dream_text=case["dream_text"], locale=case["locale"])
        entry = {"id": case["id"], "manual_review": case["review"]}
        try:
            async with asyncio.timeout(100):
                symbols = await provider.extract(body)
                result = await provider.free(body, symbols)
            entry["result"] = result.model_dump()
            prose = (result.summary + " " + result.key_connection + " " + result.reflection_question).casefold()
            entry["missing_anchor_hints"] = [a for a in case["anchors"] if a.casefold() not in prose]
        except (ProviderError, TimeoutError) as error:
            entry["error"] = error.code if isinstance(error, ProviderError) else "timeout"
        entry["telemetry"] = provider.telemetry()
        report["cases"].append(entry)
    results = [e for e in report["cases"] if "result" in e]
    for i, left in enumerate(results):
        for right in results[i+1:]:
            score = SequenceMatcher(None, left["result"]["summary"].casefold(), right["result"]["summary"].casefold()).ratio()
            if score >= .75:
                report["similar_pairs"].append({"ids": [left["id"], right["id"]], "similarity": round(score, 3)})
    path = ROOT / ".cache/quality/live-report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {path}. Human review required; lexical checks do not prove quality.")


if __name__ == "__main__":
    asyncio.run(main())
