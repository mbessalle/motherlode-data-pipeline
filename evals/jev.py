"""TypeSafe's Jev decision model on OpenRouter: closed questions answered with probabilities, no generated text.

    answers = decide({"headline": "..."}, {"is_deal": noul("Is this ...?", yes="...", no="...")})
    answers["is_deal"]["noul"]       # probability of yes, 0..1

Used where a judgment is a closed question: screening ("is this a project deal?") and checking an extraction
("is this value really this deal's cash payment?"). Jev does not do arithmetic, counting or date comparison
well (TypeSafe's guidance), so sums, splits and unit conversions stay in code and Jev judges meaning only.
It has no field for few-shot examples; examples and counterexamples go into the criteria text.

Replies are cached by exact request in evals/.cache/jev/, so a re-run costs nothing.
Docs: https://openrouter.ai/docs/guides/community/jev
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import requests

from pipeline import telemetry

URL = "https://openrouter.ai/api/alpha/decisions"
MODEL = os.getenv("JEV_MODEL", "typesafe/jev-1.14")   # pinned: thresholds are tuned to one version
CACHE = Path(__file__).parent / ".cache" / "jev"
spend = {"calls": 0, "cached": 0, "usd": 0.0, "input_tokens": 0}


def noul(instructions: str, yes: str | None = None, no: str | None = None) -> dict:
    question = {"type": "noul", "instructions": instructions}
    if yes or no:
        question["criteria"] = {"true": yes or "", "false": no or ""}
    return question


def choice(instructions: str, options: dict[str, str]) -> dict:
    return {"type": "choice", "instructions": instructions, "criteria": options}


def decide(state: dict, questions: dict, post=requests.post, cache: Path = CACHE) -> dict:
    """The answers by question name. Raises on an HTTP error, so a failed call is never read as an answer."""
    body = {"model": MODEL, "state": state, "questions": questions}
    path = cache / f"{hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:32]}.json"
    if path.exists():
        spend["cached"] += 1
        return json.loads(path.read_text())["answers"]
    attributes = {"gen_ai.operation.name": "decide", "gen_ai.provider.name": "openrouter", "gen_ai.request.model": MODEL,
                  "pipeline.llm.purpose": ",".join(questions)}
    with telemetry.span(f"decide {MODEL}", **attributes) as s:
        if telemetry.record_content():
            s.set_attribute("gen_ai.input.messages", telemetry.clip(json.dumps(state, ensure_ascii=False)))
        response = post(URL, headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"}, json=body, timeout=60)
        if response.status_code != 200:
            s.set_attribute("error.type", f"http {response.status_code}")
            raise RuntimeError(f"Jev returned HTTP {response.status_code}: {response.text[:300]}")
        data = response.json()
        usage = data.get("usage", {})
        tokens = usage.get("input_tokens", usage.get("inputTokens", 0))
        s.set_attribute("gen_ai.usage.input_tokens", tokens)
        s.set_attribute("gen_ai.response.model", data.get("model", MODEL))
        s.set_attribute("gen_ai.output.messages", json.dumps(data.get("answers", {})))
    spend["calls"] += 1
    spend["usd"] += float(usage.get("cost") or 0)
    spend["input_tokens"] += tokens
    cache.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1))
    return data["answers"]
