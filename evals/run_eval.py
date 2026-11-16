"""Offline evaluation of the extractor on real press releases.

    python -m evals.run_eval fetch                 # download the source texts into evals/.cache (not committed)
    python -m evals.run_eval score --model gpt-6.1-terra [--retry]

For each case the production prompt is run on the saved article text (no database, no MiningHub), then:
  grounding  every extracted number is checked against the article by pipeline.validate
  retry      with --retry, records that fail get the same corrected second attempt the pipeline gives them
  agreement  extracted values are compared with the analyst's spreadsheet values for the same deal

Model replies are cached per attempt, so re-scoring after a validator change costs nothing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pipeline import config, llm
from pipeline.validate import CHECKED_FIELDS, DERIVED, EMPTY, FOUND, NOT_FOUND, to_number, validate

HERE = Path(__file__).parent
CASES = HERE / "cases.jsonl"
CACHE = HERE / ".cache"
ANALYST_FIELDS = ("cash_payments_value", "exp_comit_value", "shares_amount", "share_price", "nsr_percent",
                  "interest_acquired_percent", "project_area")


def load_cases() -> list[dict]:
    return [json.loads(line) for line in CASES.read_text().splitlines() if line.strip()]


def text_path(case: dict) -> Path:
    return CACHE / "texts" / f"{case['id']}.txt"


def fetch(_args) -> None:
    import requests
    import trafilatura

    (CACHE / "texts").mkdir(parents=True, exist_ok=True)
    for case in load_cases():
        path = text_path(case)
        if not path.exists():
            html = requests.get(case["source_url"], headers={"User-Agent": "Mozilla/5.0"}, timeout=30).text
            path.write_text(trafilatura.extract(html, include_tables=True, favor_recall=True) or "")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        note = "ok" if digest == case["text_sha256_16"] else "TEXT CHANGED since the case was recorded"
        print(f"{case['id']:>7} {len(path.read_text()):>6} chars  {note}")


# Replies are cached per prompt version, so a changed extraction prompt is measured again, never read from old replies.
PROMPT_VERSION = hashlib.sha256(llm.EXTRACTOR_SYSTEM_PROMPT.encode()).hexdigest()[:8]


def extraction(client, model: str, case: dict, attempt: int, feedback: str | None) -> dict:
    path = CACHE / "extractions" / model.removeprefix("openai/") / PROMPT_VERSION / f"attempt{attempt}" / f"{case['id']}.json"
    if path.exists():
        return json.loads(path.read_text())
    text = " ".join(text_path(case).read_text().split())
    result = llm.extract_deal(client, model, text, {"project_name": case["project"]}, feedback)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=1))
    return result


TRIAGE_VERSION = hashlib.sha256(llm.TRIAGE_SYSTEM_PROMPT.encode()).hexdigest()[:8]


def project_count(case: dict, text: str) -> int:
    """How many projects the deal covers, as production decides it: the triage model lists them and code checks
    each name is written (pipeline.run.project_parts). Replies are cached per triage prompt version."""
    from pipeline.run import project_parts

    path = CACHE / "triage" / TRIAGE_VERSION / f"{case['id']}.json"
    if path.exists():
        verdict = json.loads(path.read_text())
    else:
        import openai

        settings = config.load()
        key = settings.openai_api_key or os.getenv("OPENAI_API_KEY")
        client = openai.OpenAI(api_key=key) if key else openai.OpenAI(
            base_url="https://openrouter.ai/api/v1", api_key=os.environ["OPENROUTER_API_KEY"])
        model = settings.classify_model if key else f"openai/{settings.classify_model}"
        verdict = llm.triage(client, model, case.get("title") or case["project"], case.get("company") or "", text)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(verdict))
    return len(project_parts(verdict, text)) or 1


def agrees(a, b) -> bool:
    a, b = to_number(a), to_number(b)
    return a is not None and b is not None and abs(a - b) <= max(0.011, 0.01 * abs(b))


def score(args) -> None:
    cases = [c for c in load_cases() if text_path(c).exists()]
    client = None
    if not all((CACHE / "extractions" / args.model / PROMPT_VERSION / "attempt1" / f"{c['id']}.json").exists() for c in cases) \
            or args.retry:
        import openai

        key = config.load().openai_api_key or os.getenv("OPENAI_API_KEY")
        if key:
            client = openai.OpenAI(api_key=key)
        else:              # the same model through OpenRouter, same price
            client = openai.OpenAI(base_url="https://openrouter.ai/api/v1", api_key=os.environ["OPENROUTER_API_KEY"])
            args.model = f"openai/{args.model}"

    def run_case(case: dict) -> dict:
        text = " ".join(text_path(case).read_text().split())
        count = project_count(case, text) if args.projects else 1
        first = extraction(client, args.model, case, 1, None)
        report1 = validate(first, text, count)
        final, report = first, report1
        if args.retry and not report1.passed:
            final = extraction(client, args.model, case, 2, report1.feedback())
            report = validate(final, text, count)
        return {"case": case, "first": report1, "final": report, "extraction": final, "retried": final is not first}

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(run_case, cases))

    def table(key: str) -> tuple[list[str], int, int]:
        lines, grounded_all, checked_all = [], 0, 0
        for name in CHECKED_FIELDS:
            counts = {s: sum(1 for r in results if r[key].fields[name].status == s) for s in (FOUND, DERIVED, NOT_FOUND, EMPTY)}
            checked = counts[FOUND] + counts[DERIVED] + counts[NOT_FOUND]
            grounded = counts[FOUND] + counts[DERIVED]
            grounded_all, checked_all = grounded_all + grounded, checked_all + checked
            rate = f"{100 * grounded / checked:.0f}%" if checked else "-"
            lines.append(f"| `{name}` | {counts[FOUND]} | {counts[DERIVED]} | {counts[NOT_FOUND]} | {counts[EMPTY]} | {rate} |")
        return lines, grounded_all, checked_all

    header = "| Field | Written in article | Derived by a rule | Unsupported | Empty | Supported |\n|---|---|---|---|---|---|"
    out = [f"# Eval: `{args.model}` on {len(results)} press releases\n"]
    for key, title in (("first", "First attempt"), ("final", "After the corrected retry")):
        if key == "final" and not args.retry:
            continue
        lines, grounded, checked = table(key)
        passed = sum(1 for r in results if r[key].passed)
        out += [f"## {title}\n", header, *lines, "",
                f"- Values supported by the article: **{grounded} of {checked} ({100 * grounded / checked:.0f}%)**",
                f"- Records with every value supported: **{passed} of {len(results)}**", ""]
    if args.retry:
        retried = [r for r in results if r["retried"]]
        out.append(f"- Records retried: {len(retried)}; of those, now fully supported: {sum(1 for r in retried if r['final'].passed)}\n")

    out += ["## Agreement with the analyst's spreadsheet\n", "| Field | Both filled | Agree | Model only | Analyst only |", "|---|---|---|---|---|"]
    for name in ANALYST_FIELDS:
        both = agree = model_only = analyst_only = 0
        for r in results:
            model_value, analyst_value = to_number(r["extraction"].get(name)), to_number(r["case"]["analyst"].get(name))
            if model_value and analyst_value:
                both += 1
                agree += agrees(model_value, analyst_value)
            elif model_value:
                model_only += 1
            elif analyst_value:
                analyst_only += 1
        out.append(f"| `{name}` | {both} | {agree} | {model_only} | {analyst_only} |")
    report_text = "\n".join(out) + "\n"
    print(report_text)
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / f"{args.model.removeprefix('openai/')}{'-retry' if args.retry else ''}.md").write_text(report_text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("fetch").set_defaults(run=fetch)
    s = sub.add_parser("score")
    s.add_argument("--model", default="gpt-6.1-terra")
    s.add_argument("--retry", action="store_true")
    s.add_argument("--projects", action="store_true",
                   help="give the validator the triage model's number of projects, as the pipeline does")
    s.set_defaults(run=score)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
