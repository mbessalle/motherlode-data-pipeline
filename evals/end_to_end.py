"""Run a random sample of the analyst's deals through the whole pipeline, and compare what it would store
with what the analyst stored.

    python -m evals.end_to_end estimate --sample 30 --seed 7        # free: which cases, and what the run would cost
    python -m evals.end_to_end run      --sample 30 --seed 7        # needs OPENAI_API_KEY for replies not yet cached
    python -m evals.end_to_end run --openrouter ...                 # the same models through OpenRouter
    python -m evals.jev_verify RUN_DIR                              # Jev checks each extracted value (evals/jev_verify.py)

The ground truth is the analyst's own table (Antonio Celis, Motherlode Consulting) and the announcement PDF behind
each row, prepared in the Snowflake version of this project (`evals/pdf_cases.jsonl` there pairs each row with
its most informative PDF and records which analyst values are written in it). Those files are the client's and
are read from that checkout; nothing from them is committed here.

What runs is `pipeline.run.run`, unchanged, with three substitutions and nothing else:
  feed       the sampled rows as listing entries (headline, company, date from the analyst's row)
  search     returns the row's PDF as the only search result; reading it (text layer, OCR) and the identity
             test that must accept it are the production code
  database   `MemoryStore`, empty at the start, so every write is an insert and nothing leaves the machine
The model calls are real and are cached by their exact request, so a second run of the same sample is free.

Two switches turn off a production gate, to measure the steps after it on more entries (both are recorded in
sample.json and summary.md, and both are off by default):
  --no-headline-filter   every entry goes to search, not only headlines with deal words
  --no-date-check        the source need not write a date near the listing date. Needed because the listing date
                         here is the analyst's `news_date`, and in several rows its day and month are swapped
                         (row 306: 2025-04-03 for a release dated March 4, 2025)

Each run writes to its own folder (default evals/.cache/e2e/<seed>-<time>/):
  sample.json          the seed and the case ids, so the sample can be reproduced
  run_report.json      what production writes: per entry the search, triage, extraction, validation, outcome
  review_queue.jsonl   values withheld by the validator
  spans.jsonl          every OpenTelemetry span, with the prompt and reply of each model call
  stdout.log           the [pipeline] progress lines
  stored.json          the rows the pipeline wrote to the in-memory database
  comparison.jsonl     per entry and field: pipeline value, analyst value, agree or not, and whether the PDF
                       writes the analyst's value
  summary.md           the counts, and the spend of this run
Set OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318 to see the same spans in Jaeger as well.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import datetime as dt
import hashlib
import io
import json
import os
import random
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pipeline import config, telemetry
from pipeline.pdf import pdf_text
from pipeline.prompts import CLASSIFIER_SYSTEM_PROMPT, EXTRACTOR_SYSTEM_PROMPT, TRIAGE_SYSTEM_PROMPT
from pipeline.run import run
from pipeline.search import Searcher
from pipeline.store import MemoryStore
from pipeline.validate import to_number

HERE = Path(__file__).parent
SNOWFLAKE = Path.home() / "snowflake-motherload"
DEFAULT_CASES = SNOWFLAKE / "evals" / "pdf_cases.jsonl"
DEFAULT_ROWS = SNOWFLAKE / "data" / "client" / "oceania_export_2026-11-16.csv"
DEFAULT_PDFS = SNOWFLAKE / "data" / "source_pdfs" / "projects"
CACHE = HERE / ".cache" / "e2e"
# USD per million tokens (input, output). Third-party price listings of November 2026; OpenAI's page was not read.
PRICES = {"gpt-6.1-terra": (1.00, 5.00), "gpt-6-luna": (0.10, 0.50)}
# Fields the analyst scrapes from the announcement, compared within 1%; the rest are market data or calculated.
COMPARED = ("cash_payments_value", "exp_comit_value", "shares_amount", "nsr_percent", "interest_acquired_percent",
            "project_area", "drilling_amount", "aggregate_deal_value", "shares_value", "share_price")
CURRENCIES = {"cash_payments_value": "cash_payments_currency", "exp_comit_value": "exp_comit_currency",
              "aggregate_deal_value": "aggregate_deal_currency", "shares_value": "shares_currency"}


# --- inputs ---------------------------------------------------------------------------------------------------

def load_cases(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_rows(path: Path) -> dict[int, dict]:
    """The analyst's rows by id. The export is one CSV line holding the table's rows as JSON."""
    csv.field_size_limit(10**9)
    rows = json.loads(next(csv.DictReader(path.open()))["rows"])
    return {row["id"]: row for row in rows}


def choose(cases: list[dict], size: int, seed: int) -> list[dict]:
    """A reproducible random sample. Cases whose PDF the case builder did not pick have no file and are left out."""
    usable = sorted((c for c in cases if c.get("file")), key=lambda c: c["id"])
    return random.Random(seed).sample(usable, min(size, len(usable)))


def listing_entry(case: dict, row: dict) -> dict:
    """What the news feed would have given for this deal: headline, company, date. The id is the row id."""
    return {"id": case["row_id"], "title": row.get("news_title") or "", "company": (row.get("owner_name") or "").lower(),
            "date": f"{row['news_date']}T00:00:00Z" if row.get("news_date") else "", "listing_link": row.get("news_link") or ""}


def hectares(value, unit) -> float | None:
    number = to_number(value)
    if number is None:
        return None
    unit = (unit or "ha").strip().lower().replace("²", "2")
    return {"ha": 1, "hectares": 1, "hectare": 1, "km2": 100, "sq km": 100, "acres": 0.404686}.get(unit, 0) * number or None


# --- substitutions --------------------------------------------------------------------------------------------

class LocalSearcher(Searcher):
    """The production searcher, with the web search answered by the case's PDF on disk."""

    def __init__(self, pdf_of: dict[int, Path]):
        super().__init__("local", get=self._get)
        self.pdf_of, self.current = pdf_of, None

    def find_announcement(self, article: dict):
        self.current = self.pdf_of[article["id"]]
        return super().find_announcement(article)

    def results(self, query: str) -> list[dict]:
        self.searches += 1
        return [{"link": self.current.as_uri()}]

    @staticmethod
    def _get(url, headers=None, timeout=None):
        path = Path(url.removeprefix("file://"))
        return SimpleNamespace(status_code=200, headers={"content-type": "application/pdf"}, content=path.read_bytes())


class CachedClient:
    """An OpenAI client stand-in: a reply is read from the cache when the exact request was made before,
    otherwise it is requested and saved. Counts calls and the tokens of the requests that cost money."""

    def __init__(self, inner, cache_dir: Path):
        self.inner, self.cache_dir = inner, cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.cached, self.paid, self.tokens = 0, 0, Counter()

    def create(self, **request):
        key = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()[:32]
        path = self.cache_dir / f"{key}.json"
        if path.exists():
            self.cached += 1
            saved = json.loads(path.read_text())
        else:
            if self.inner is None:
                raise RuntimeError("reply not cached and no OPENAI_API_KEY")   # recorded on the span as error.type
            response = self.inner.chat.completions.create(**request)
            usage = getattr(response, "usage", None)
            saved = {"model": response.model, "content": response.choices[0].message.content,
                     "usage": {"prompt_tokens": usage.prompt_tokens, "completion_tokens": usage.completion_tokens} if usage else None}
            path.write_text(json.dumps(saved, ensure_ascii=False, indent=1))
            self.paid += 1
            if usage:
                self.tokens[(request["model"], "in")] += usage.prompt_tokens
                self.tokens[(request["model"], "out")] += usage.completion_tokens
        return SimpleNamespace(model=saved["model"], choices=[SimpleNamespace(message=SimpleNamespace(content=saved["content"]))],
                               usage=SimpleNamespace(**saved["usage"]) if saved["usage"] else None)

    def spend_usd(self) -> float:
        return sum(count * PRICES.get(model.removeprefix("openai/"), (0, 0))[0 if way == "in" else 1] / 1e6
                   for (model, way), count in self.tokens.items())


# --- comparison -----------------------------------------------------------------------------------------------

def agrees(a, b) -> bool:
    return a is not None and b is not None and abs(a - b) <= max(0.011, 0.01 * abs(b))


def compare(case: dict, row: dict, entry: dict | None, stored: dict | None) -> list[dict]:
    """One line per compared field. `in_document` says whether the analyst's value can be read in the PDF
    (written, derived by a named rule, or no), so a disagreement can be told apart from a missing source."""
    lines = []
    for name in COMPARED:
        if name == "project_area":
            mine = hectares((stored or {}).get(name), (stored or {}).get("project_area_unit"))
            theirs = hectares(row.get(name), row.get("project_area_unit"))
        else:
            mine, theirs = to_number((stored or {}).get(name)), to_number(row.get(name))
        if mine is None and theirs is None:
            continue
        derived = case.get("derived_in_document", {}).get(name)
        lines.append({
            "case_id": case["id"], "row_id": case["row_id"], "outcome": (entry or {}).get("outcome", "not_in_report"),
            "field": name, "pipeline": mine, "analyst": theirs,
            "pipeline_currency": (stored or {}).get(CURRENCIES.get(name, ""), None), "analyst_currency": row.get(CURRENCIES.get(name, "")),
            "verdict": ("agree" if agrees(mine, theirs) else "pipeline_only" if theirs is None
                        else "analyst_only" if mine is None else "disagree"),
            "in_document": "written" if name in case.get("written_in_document", []) else f"derived:{derived}" if derived else "no",
            "withheld": name in ((entry or {}).get("withheld") or {}),
        })
    return lines


def summary_text(seed: int, cases: list[dict], report: list[dict], comparison: list[dict], client: CachedClient) -> str:
    outcomes = Counter(e["outcome"] for e in report)
    verdicts = Counter((c["verdict"], c["in_document"] != "no") for c in comparison)
    by_field = {}
    for c in comparison:
        by_field.setdefault(c["field"], Counter())[c["verdict"]] += 1
    out = [f"# End-to-end sample: seed {seed}, {len(cases)} cases\n", "## How each entry left the pipeline\n",
           *[f"- {name}: {count}" for name, count in outcomes.most_common()],
           f"- not reached (dropped before search, e.g. by the headline filter): {len(cases) - len(report)}", "",
           "## Stored values against the analyst's\n", "| Field | Agree | Disagree | Pipeline only | Analyst only |",
           "|---|---|---|---|---|",
           *[f"| `{f}` | {v['agree']} | {v['disagree']} | {v['pipeline_only']} | {v['analyst_only']} |" for f, v in by_field.items()], "",
           f"- Disagreements where the analyst's value is in the PDF: {verdicts[('disagree', True)]}",
           f"- Analyst values in the PDF that the pipeline did not store: {verdicts[('analyst_only', True)]}", "",
           "## Model calls\n", f"- cached: {client.cached}, paid: {client.paid}, spend of this run: {client.spend_usd():.4f} USD",
           *[f"- {model} {way}: {count} tokens" for (model, way), count in sorted(client.tokens.items())]]
    return "\n".join(out) + "\n"


# --- commands -------------------------------------------------------------------------------------------------

def prepared(args) -> tuple[list[dict], dict[int, dict], dict[int, Path]]:
    cases = choose(load_cases(args.cases), args.sample, args.seed)
    rows = load_rows(args.rows)
    pdf_of = {c["row_id"]: args.pdfs / c["project"] / c["file"] for c in cases}
    missing = [str(p) for p in pdf_of.values() if not p.exists()]
    if missing:
        raise SystemExit(f"PDFs not found: {missing[:3]}")
    return cases, rows, pdf_of


def estimate(args) -> None:
    """Upper bound of the spend: every call uncached, two extraction attempts per case. Tokens = chars / 4."""
    cases, rows, pdf_of = prepared(args)
    extract_in = triage_in = 0
    for case in cases:
        text, _ = pdf_text(pdf_of[case["row_id"]].read_bytes(), ocr=lambda content, pages: [""] * len(pages))
        chars = len(" ".join(text.split()))
        extract_in += (len(EXTRACTOR_SYSTEM_PROMPT) + min(chars, 15000) + 600) / 4
        triage_in += (len(TRIAGE_SYSTEM_PROMPT) + min(chars, 4000) + len(CLASSIFIER_SYSTEM_PROMPT) + 900) / 4
    terra, luna = PRICES[args.model], PRICES[args.classify_model]
    one = extract_in * terra[0] / 1e6 + len(cases) * 1300 * terra[1] / 1e6
    small = triage_in * luna[0] / 1e6 + len(cases) * 250 * luna[1] / 1e6
    print(f"{len(cases)} cases, seed {args.seed}: {[c['id'] for c in cases]}")
    print(f"one extraction attempt each: {extract_in:,.0f} input tokens, about {one:.2f} USD; with a retry on every case "
          f"at most {2 * one:.2f} USD; triage and region calls about {small:.3f} USD")


def run_sample(args, inner=None) -> Path:
    """`inner` is the OpenAI client for replies not in the cache; by default one is made when OPENAI_API_KEY is set."""
    cases, rows, pdf_of = prepared(args)
    out = (args.out or CACHE / f"{args.seed}-{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%SZ}").resolve()   # run() changes into it
    out.mkdir(parents=True, exist_ok=True)
    gates_off = [name for name, off in (("headline filter", args.no_headline_filter), ("date check", args.no_date_check)) if off]
    # The full case records are saved with the run: the case file is rebuilt in its own checkout and ids can change.
    (out / "sample.json").write_text(json.dumps({"seed": args.seed, "size": len(cases), "gates_off": gates_off,
                                                 "cases": [c["id"] for c in cases], "case_records": cases}, indent=1))
    os.environ.setdefault("PIPELINE_TRACE_FILE", str(out / "spans.jsonl"))
    os.environ.setdefault("PIPELINE_TRACE_CONTENT", "1")

    if inner is None and args.openrouter:
        import openai

        inner = openai.OpenAI(base_url="https://openrouter.ai/api/v1", api_key=os.environ["OPENROUTER_API_KEY"])
        args.model, args.classify_model = f"openai/{args.model}", f"openai/{args.classify_model}"
    elif inner is None and os.getenv("OPENAI_API_KEY"):
        import openai

        inner = openai.OpenAI()
    client = CachedClient(inner, args.replies or CACHE / "replies")
    settings = config.Settings(None, None, None, None, None, os.getenv("OPENAI_API_KEY"), args.model, args.classify_model,
                               2, str(out / "review_queue.jsonl"), 0, str(out / "run_report.json"), None)
    feed = [listing_entry(c, rows[c["row_id"]]) for c in cases]
    store, log = MemoryStore(), io.StringIO()
    here = Path.cwd()
    os.chdir(out)                      # run() keeps its state file in the working folder
    try:
        with contextlib.ExitStack() as gates, contextlib.redirect_stdout(Tee(sys.stdout, log)):
            if args.no_headline_filter:
                gates.enter_context(mock.patch("pipeline.sources.looks_like_a_deal", lambda title: True))
            if args.no_date_check:
                gates.enter_context(mock.patch("pipeline.search.dated_near", lambda text, listed: True))
            # newest first, as the sitemap gives them; run() treats every entry as new (no state file here)
            run(settings, store=store, client=client, searcher=LocalSearcher(pdf_of), fetch=lambda: feed[::-1])
    finally:
        os.chdir(here)
        telemetry.shutdown()
    (out / "stdout.log").write_text(log.getvalue())
    (out / "stored.json").write_text(json.dumps(store.tables, indent=1, default=str))

    report = json.loads((out / "run_report.json").read_text()) if (out / "run_report.json").exists() else []
    by_id = {e["id"]: e for e in report}
    comparison = []
    for case in cases:
        entry = by_id.get(case["row_id"])
        stored = None
        if entry and entry.get("table"):
            stored = store.fetch_record(entry["table"], entry.get("project_name"))
        comparison += compare(case, rows[case["row_id"]], entry, stored)
    (out / "comparison.jsonl").write_text("".join(json.dumps(c, default=str) + "\n" for c in comparison))
    text = summary_text(args.seed, cases, report, comparison, client)
    if gates_off:
        text = text.replace("\n\n", f"\n\nProduction gates turned off for this run: {', '.join(gates_off)}.\n\n", 1)
    (out / "summary.md").write_text(text)
    print(text + f"\nWritten to {out}")
    return out


class Tee(io.TextIOBase):
    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for stream in self.streams:
            stream.write(text)
        return len(text)

    def flush(self):
        for stream in self.streams:
            stream.flush()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("estimate", "run"))
    parser.add_argument("--sample", type=int, default=30)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--rows", type=Path, default=DEFAULT_ROWS)
    parser.add_argument("--pdfs", type=Path, default=DEFAULT_PDFS)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--replies", type=Path, help="cache folder for model replies (default evals/.cache/e2e/replies)")
    parser.add_argument("--model", default="gpt-6.1-terra")
    parser.add_argument("--classify-model", default="gpt-6-luna")
    parser.add_argument("--openrouter", action="store_true",
                        help="send the model calls through OpenRouter (OPENROUTER_API_KEY), same models and prices")
    parser.add_argument("--no-headline-filter", action="store_true")
    parser.add_argument("--no-date-check", action="store_true")
    args = parser.parse_args(argv)
    (estimate if args.command == "estimate" else run_sample)(args)


if __name__ == "__main__":
    main()
