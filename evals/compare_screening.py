"""Compare two ways of screening an announcement ("is it a project deal?", "which continent?").

    python -m evals.compare_screening [--analyst-rows PATH]

  chat       the pipeline's current path: two JSON-mode chat calls (triage, then region)
  decisions  one call to OpenAI's Decisions API with a predicate and a choice question
  jev        one call to TypeSafe's Jev decision model on OpenRouter (OPENROUTER_API_KEY), same two questions
A second section compares the free headline word filter with Jev reading the headline alone.

Both run on the same labelled announcements, one after the other, so latency is comparable.
Replies are cached, so re-scoring costs nothing; delete evals/.cache/screening/replies to re-measure.
"""
from __future__ import annotations

import json
import os
import statistics
import time
from pathlib import Path

from evals import end_to_end, jev
from pipeline import config, decisions, llm, sources

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


JEV_DEAL = jev.noul(
    "Does the announcement report a transaction over a specific mineral project or property?",
    yes="An acquisition, sale, option, earn-in, farm-in or joint venture over a named project, property, tenement or "
        "claims, or a merger or takeover that transfers named projects; also when the announcement completes, amends "
        "or funds such a transaction, or adds tenements or claims to a project by agreement with another party.",
    no="It only reports drilling or assay results, a financing or placement, production or sales of metal, a "
       "shareholder vote, or an operational update, even if an earlier deal is mentioned in passing; or new ground "
       "the company pegged or applied for itself, with no other party.")
JEV_CONTINENT = jev.choice("On which continent is the project in the transaction located?",
                           {**{name: f"The project is in {name}." for name in config.CONTINENT_TO_TABLE},
                            "unknown": "The text does not say where the project is."})
JEV_HEADLINE = jev.noul(
    "Could this headline be about a transaction over a specific mineral project or property?",
    yes="The headline names or suggests an acquisition, sale, option, earn-in, farm-in, joint venture, merger, "
        "takeover, or new tenements, claims or ground added by agreement, or the completion or amendment of one.",
    no="The headline is about drilling or assay results, a resource estimate, a financing or placement, production, "
       "appointments, a quarterly report, or another operational update.")


def run_jev(case: dict, text: str) -> dict:
    start = time.perf_counter()
    before = jev.spend["usd"], jev.spend["input_tokens"]
    answers = jev.decide({"headline": case["title"], "company": case["company"], "announcement_start": text[:decisions.SCREEN_CHARS]},
                         {"project_deal": JEV_DEAL, "continent": JEV_CONTINENT})
    continent = answers["continent"]["choice"]
    return {"deal": answers["project_deal"]["noul"] >= 0.5, "probability": answers["project_deal"]["noul"],
            "continent": None if continent == "unknown" else continent, "seconds": time.perf_counter() - start,
            "input_tokens": jev.spend["input_tokens"] - before[1], "output_tokens": 0, "calls": 1,
            "usd": jev.spend["usd"] - before[0]}


def jev_headline(title: str, company: str) -> float:
    return jev.decide({"headline": title, "company": company}, {"headline_deal": JEV_HEADLINE})["headline_deal"]["noul"]


def summarise(name: str, cases: list[dict], results: list[dict]) -> dict:
    deals = [(c, r) for c, r in zip(cases, results, strict=True) if c["label_deal"]]
    tokens_in = statistics.mean(r["input_tokens"] for r in results)
    tokens_out = statistics.mean(r["output_tokens"] for r in results)
    usd = (1000 * statistics.mean(r["usd"] for r in results) if all("usd" in r for r in results)
           else 1000 * (tokens_in * LUNA_PRICE[0] + tokens_out * LUNA_PRICE[1]) / 1_000_000)
    return {
        "name": name, "calls": results[0]["calls"],
        "deal_correct": sum(r["deal"] == c["label_deal"] for c, r in zip(cases, results, strict=True)), "n": len(cases),
        "continent_correct": sum(r["continent"] == c["label_continent"] for c, r in deals), "n_deals": len(deals),
        "median_seconds": statistics.median(r["seconds"] for r in results),
        "tokens_in": tokens_in, "tokens_out": tokens_out,
        "usd_per_1000": usd,
    }


class LazyOpenAI:
    """The OpenAI client, created on first use: re-scoring from cached replies needs no key."""

    def __init__(self):
        self._client = None

    @property
    def chat(self):
        if self._client is None:
            import openai

            self._client = openai.OpenAI(api_key=config.load().openai_api_key or os.environ["OPENAI_API_KEY"])
        return self._client.chat


def headline_filters(cases: list[dict], analyst_rows: Path | None) -> list[str]:
    """The free word filter against Jev on the headline alone, on the labelled cases and on every headline of the
    analyst's deal table (each row there is a deal the analyst recorded, so only misses can be counted)."""
    out = ["", "## Headline filter: words against Jev on the headline alone\n",
           "| Headlines | Word filter passes | Jev passes (p >= 0.5) | Jev passes (p >= 0.2) |", "|---|---|---|---|"]
    sets = [("Labelled deals", [(c["title"], c["company"]) for c in cases if c["label_deal"]]),
            ("Labelled non-deals", [(c["title"], c["company"]) for c in cases if not c["label_deal"]])]
    if analyst_rows:
        rows = end_to_end.load_rows(analyst_rows).values()
        sets.append(("Analyst's deal table", [(r["news_title"], (r.get("owner_name") or "").lower())
                                              for r in rows if r.get("news_title")]))
    missed = []
    for name, headlines in sets:
        p = [jev_headline(title, company) for title, company in headlines]
        words = [sources.looks_like_a_deal(title) for title, _ in headlines]
        out.append(f"| {name} ({len(headlines)}) | {sum(words)} | {sum(x >= 0.5 for x in p)} | {sum(x >= 0.2 for x in p)} |")
        if name == "Analyst's deal table":
            missed = [(t, w, x) for (t, _), w, x in zip(headlines, words, p, strict=True) if not w or x < 0.5]
    if missed:
        out += ["", "Analyst headlines that a filter drops (word filter, Jev p):", ""]
        out += [f"- {t[:90]} ({'passes' if w else 'dropped'}, {x:.2f})" for t, w, x in missed[:40]]
    return out


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--analyst-rows", type=Path, help="the analyst's table export, for the headline filter section")
    args = parser.parse_args()
    settings = config.load()
    client = LazyOpenAI()
    cases = [json.loads(line) for line in CASES.read_text().splitlines() if line.strip()]
    cases = [c for c in cases if (CACHE / f"{c['id']}.txt").exists()]
    chat, decide, jevs = [], [], []
    for case in cases:
        text = (CACHE / f"{case['id']}.txt").read_text()
        chat.append(cached("chat", case, lambda c=case, t=text: run_chat(client, settings.classify_model, c, t)))
        decide.append(cached("decisions", case, lambda c=case, t=text: run_decisions(
            settings.openai_api_key or os.environ["OPENAI_API_KEY"], c, t)))
        jevs.append(cached(f"jev-{jev.MODEL.replace('/', '_')}", case, lambda c=case, t=text: run_jev(c, t)))

    rows = [summarise(f"Two chat calls (`{settings.classify_model}`)", cases, chat),
            summarise(f"One Decisions API call (`{decisions.DECISION_MODEL}`)", cases, decide),
            summarise(f"One Jev call (`{jev.MODEL}`, OpenRouter)", cases, jevs)]
    out = [f"# Screening comparison on {len(cases)} labelled announcements\n",
           "| | Calls | Deal or not, correct | Continent, correct | Median latency | Tokens in / out | Cost per 1,000 |",
           "|---|---|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {r['name']} | {r['calls']} | {r['deal_correct']} of {r['n']} | {r['continent_correct']} of {r['n_deals']} | "
                   f"{r['median_seconds']:.2f} s | {r['tokens_in']:.0f} / {r['tokens_out']:.0f} | ${r['usd_per_1000']:.2f} |")
    disagreements = [(c, a, b, j) for c, a, b, j in zip(cases, chat, decide, jevs, strict=True)
                     if len({(r["deal"], r["continent"]) for r in (a, b, j)}) > 1 or any(r["deal"] != c["label_deal"] for r in (a, b, j))]
    out += ["", f"Labels: {sum(c['label_deal'] for c in cases)} project deals, {sum(not c['label_deal'] for c in cases)} not.",
            f"Chat and Decisions API cost uses ${LUNA_PRICE[0]:.2f} / ${LUNA_PRICE[1]:.2f} per million input / output tokens; "
            "Jev cost is the `usage.cost` OpenRouter reports (input tokens only; output is free). Jev's examples and "
            "counterexamples are in its criteria text (JEV_DEAL in this file); it has no few-shot field.", "",
            "## Cases where a method was wrong or the methods disagreed\n"]
    for c, a, b, j in disagreements:
        out.append(f"- `{c['id']}` {c['title'][:70]}: label deal={c['label_deal']}, {c['label_continent']}; "
                   f"chat deal={a['deal']}, {a['continent']}; decisions deal={b['deal']} (p={b.get('probability', 0):.2f}), "
                   f"{b['continent']}; jev deal={j['deal']} (p={j['probability']:.2f}), {j['continent']}")
    out += headline_filters(cases, args.analyst_rows)
    text = "\n".join(out) + "\n"
    print(text)
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / "screening-comparison.md").write_text(text)


if __name__ == "__main__":
    main()
