"""Compare two ways of screening an announcement ("is it a project deal?", "which continent?").

    python -m evals.compare_screening

  chat       the pipeline's current path: two JSON-mode chat calls (triage, then region)
  decisions  one call to OpenAI's Decisions API with a predicate and a choice question

Both run on the same labelled announcements, one after the other, so latency is comparable.
Replies are cached, so re-scoring costs nothing; delete evals/.cache/screening/replies to re-measure.
"""
from __future__ import annotations

import json
import os
import statistics
import time
from pathlib import Path

from pipeline import config, decisions, llm

HERE = Path(__file__).parent
CASES = HERE / "screening_cases.jsonl"
CACHE = HERE / ".cache" / "screening"
# USD per million tokens (input, output) for gpt-6-luna, from public price listings in November 2026.
LUNA_PRICE = (0.10, 0.50)


class UsageRecorder:
    """Wraps the OpenAI client to record token usage of the chat calls."""

    def __init__(self, client):
        self._client, self.input_tokens, self.output_tokens = client, 0, 0
        self.chat = type("Chat", (), {"completions": type("Completions", (), {"create": self._create})()})()

    def _create(self, **kwargs):
        response = self._client.chat.completions.create(**kwargs)
        self.input_tokens += response.usage.prompt_tokens
        self.output_tokens += response.usage.completion_tokens
        return response


def cached(name: str, case: dict, compute):
    path = CACHE / "replies" / name / f"{case['id']}.json"
    if path.exists():
        return json.loads(path.read_text())
    result = compute()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result))
    return result


def run_chat(client, model: str, case: dict, text: str) -> dict:
    recorder = UsageRecorder(client)
    start = time.perf_counter()
    verdict = llm.triage(recorder, model, case["title"], case["company"], text)
    region = llm.classify_location(recorder, model, f"tags: '', title: '{case['title']}', description: '{text[:600]}'")
    return {"deal": verdict["is_project_deal"], "continent": region["continent"], "seconds": time.perf_counter() - start,
            "input_tokens": recorder.input_tokens, "output_tokens": recorder.output_tokens, "calls": 2}


def run_decisions(api_key: str, case: dict, text: str) -> dict:
    start = time.perf_counter()
    result = decisions.screen(api_key, case["title"], case["company"], text)
    return {"deal": result["is_project_deal"], "continent": result["continent"], "seconds": time.perf_counter() - start,
            "input_tokens": result["input_tokens"], "output_tokens": result["output_tokens"], "calls": 1,
            "probability": result["probability"]}


def summarise(name: str, cases: list[dict], results: list[dict]) -> dict:
    deals = [(c, r) for c, r in zip(cases, results, strict=True) if c["label_deal"]]
    tokens_in = statistics.mean(r["input_tokens"] for r in results)
    tokens_out = statistics.mean(r["output_tokens"] for r in results)
    return {
        "name": name, "calls": results[0]["calls"],
        "deal_correct": sum(r["deal"] == c["label_deal"] for c, r in zip(cases, results, strict=True)), "n": len(cases),
        "continent_correct": sum(r["continent"] == c["label_continent"] for c, r in deals), "n_deals": len(deals),
        "median_seconds": statistics.median(r["seconds"] for r in results),
        "tokens_in": tokens_in, "tokens_out": tokens_out,
        "usd_per_1000": 1000 * (tokens_in * LUNA_PRICE[0] + tokens_out * LUNA_PRICE[1]) / 1_000_000,
    }


def main() -> None:
    import openai

    settings = config.load()
    api_key = settings.openai_api_key or os.environ["OPENAI_API_KEY"]
    client = openai.OpenAI(api_key=api_key)
    cases = [json.loads(line) for line in CASES.read_text().splitlines() if line.strip()]
    cases = [c for c in cases if (CACHE / f"{c['id']}.txt").exists()]
    chat, decide = [], []
    for case in cases:
        text = (CACHE / f"{case['id']}.txt").read_text()
        chat.append(cached("chat", case, lambda c=case, t=text: run_chat(client, settings.classify_model, c, t)))
        decide.append(cached("decisions", case, lambda c=case, t=text: run_decisions(api_key, c, t)))

    rows = [summarise(f"Two chat calls (`{settings.classify_model}`)", cases, chat),
            summarise(f"One Decisions API call (`{decisions.DECISION_MODEL}`)", cases, decide)]
    out = [f"# Screening comparison on {len(cases)} labelled announcements\n",
           "| | Calls | Deal or not, correct | Continent, correct | Median latency | Tokens in / out | Cost per 1,000 |",
           "|---|---|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {r['name']} | {r['calls']} | {r['deal_correct']} of {r['n']} | {r['continent_correct']} of {r['n_deals']} | "
                   f"{r['median_seconds']:.2f} s | {r['tokens_in']:.0f} / {r['tokens_out']:.0f} | ${r['usd_per_1000']:.2f} |")
    disagreements = [(c, a, b) for c, a, b in zip(cases, chat, decide, strict=True)
                     if (a["deal"], a["continent"]) != (b["deal"], b["continent"]) or a["deal"] != c["label_deal"] or b["deal"] != c["label_deal"]]
    out += ["", f"Labels: {sum(c['label_deal'] for c in cases)} project deals, {sum(not c['label_deal'] for c in cases)} not.",
            f"Cost uses ${LUNA_PRICE[0]:.2f} / ${LUNA_PRICE[1]:.2f} per million input / output tokens.", "",
            "## Cases where a method was wrong or the two disagreed\n"]
    for c, a, b in disagreements:
        out.append(f"- `{c['id']}` {c['title'][:70]}: label deal={c['label_deal']}, {c['label_continent']}; "
                   f"chat deal={a['deal']}, {a['continent']}; decisions deal={b['deal']} (p={b.get('probability', 0):.2f}), {b['continent']}")
    text = "\n".join(out) + "\n"
    print(text)
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / "screening-comparison.md").write_text(text)


if __name__ == "__main__":
    main()
