"""The last tier: a frontier model reads the whole document, but only for the deals the cheap tiers could not settle.

    python -m evals.escalate estimate RUN_DIR     # which deals would go up, and how many tokens
    python -m evals.escalate run RUN_DIR          # Claude Code (`claude -p --model opus`); replies cached

Three tiers, each only for what the one before could not settle:
  code    finds every number, does every calculation                     free
  Jev     judges meaning with probabilities (evals/cell_check.py)       about 0.00004 USD a question
  Opus    reads the whole document like an analyst, for flagged deals    the Claude subscription, not an API key:
          each deal is one `claude -p` call with no tools, a JSON schema for the answer, and no saved session
A deal goes up when any of its cells was sent to review or corrected, or its buyer was not accepted. The model
gets the client's rules, the flagged cells with what code and Jev found, and the document. It answers each cell
with a verdict, the value, an exact quote and a reason, and names the buyer.

Code then checks the model, as it checks everything else: a quote must be in the document, and the value must
be written in the quote or follow from the quote's numbers by a rule. An answer that fails is kept but marked
`quote_checked: false` and the cell stays in review.

Writes RUN_DIR/escalations.jsonl and a section of RUN_DIR/summary.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from evals.documents import document_text
from pipeline import telemetry, validate
from pipeline.search import company_words
from pipeline.validate import to_number

MODEL = os.getenv("ESCALATE_MODEL", "opus")          # a Claude Code model alias, run on the subscription
MAX_DOCUMENT_CHARS = 60_000        # about 15,000 tokens; a longer document is cut, and the cut is stated
TIMEOUT_SECONDS = 600
CACHE = Path(__file__).parent / ".cache" / "escalate"

RULES = """You check a mining deal database row against its source announcement. The client's rules (Motherlode
Consulting field definitions):
- owner_name, owner_ticker: the BUYER, the company that acquires or can earn the interest.
- cash_payments_value: the sum of ALL cash payments for the project in this deal, including deferred, option and
  milestone payments. exp_comit_value: the sum of exploration expenditure commitments; a commitment in metres of
  drilling is converted at 250 USD per metre.
- shares_amount: the buyer's shares issued to the vendor as consideration (all tranches). shares_value: the value
  of those shares, as stated, or shares_amount x share_price. share_price: market data on the news date; it is
  normally not in the announcement.
- aggregate_deal_value = cash + exploration commitment + shares value, with no double count of a combined figure.
- A value shared by N projects is split equally: each project row holds total / N.
- interest_acquired_percent: the final interest the buyer acquires or can earn. nsr_percent: the royalty created,
  retained or acquired in this deal (not the part that can be bought back). project_area: the project's own area.
- Only the transaction being announced counts, not earlier, legacy or superseded agreements, and not financings.
Give `unit` for every value: the currency code for money, "ha" or "km2" for project_area as you give it, null
otherwise. Be rigorous: read the whole document, quote the exact words that support each value, and say "unsure" rather
than guess."""

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["cells", "buyer"], "properties": {
    "cells": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                         "required": ["field", "verdict", "value", "unit", "quote", "reason"],
                                         "properties": {"field": {"type": "string"},
                                                        "unit": {"type": ["string", "null"]},
                                                        "verdict": {"type": "string", "enum": ["correct", "wrong", "unsure"]},
                                                        "value": {"type": ["number", "null"]},
                                                        "quote": {"type": "string"}, "reason": {"type": "string"}}}},
    "buyer": {"type": "object", "additionalProperties": False, "required": ["name", "quote", "reason"],
              "properties": {"name": {"type": ["string", "null"]}, "quote": {"type": "string"}, "reason": {"type": "string"}}}}}


def flagged(run_dir: Path) -> dict[int, dict]:
    """Rows with at least one cell not accepted, or a buyer not accepted, keyed by (deal id, project): a deal over
    several projects has one row per project. {(row_id, project): {'cells': [...], 'party': {...}}}."""
    out: dict[int, dict] = {}
    for line in (run_dir / "cells.jsonl").read_text().splitlines():
        cell = json.loads(line)
        if cell["action"] != "accept":
            out.setdefault((cell["row_id"], cell["project"]), {"cells": [], "party": None})["cells"].append(cell)
    for line in (run_dir / "parties.jsonl").read_text().splitlines():
        party = json.loads(line)
        key = (party["row_id"], party["project"])
        if party.get("action") in ("review", "correct"):
            out.setdefault(key, {"cells": [], "party": None})["party"] = party
        elif key in out:
            out[key]["party"] = party
    return out


def document(run_dir: Path, key: tuple[int, str]) -> tuple[str, str]:
    """(project name, document text as the pipeline read it, cut at MAX_DOCUMENT_CHARS with a note)."""
    row_id, project = key
    entries = [e for e in json.loads((run_dir / "run_report.json").read_text()) if e["id"] == row_id]
    entry = next((e for e in entries if e.get("project_name") == project), entries[0])
    text = document_text(entry["source_url"])   # cached: OCR once per document
    if len(text) > MAX_DOCUMENT_CHARS:
        text = text[:MAX_DOCUMENT_CHARS] + f" [... document cut here: {len(text):,} characters in all]"
    return entry.get("project_name") or "", text


def prompt(project: str, flags: dict, text: str) -> str:
    lines = [f"Project: {project}", "", "Flagged cells (what code and the cheap checker found):"]
    for c in flags["cells"]:
        proposal = f"; cheap checker proposes {c['proposal']:,.10g} (P {c.get('p_proposal') or 0:.2f})" if c.get("proposal") else ""
        p = "-" if c.get("p_correct") is None else f"{c['p_correct']:.2f}"
        lines.append(f"- {c['field']} = {c['value']:,.10g} {c.get('unit') or ''}: source {c['source']}; P(correct) {p}{proposal}")
    party = flags.get("party") or {}
    lines.append(f"- owner_name = {party.get('owner_name')}; P(owner is buyer) {party.get('p_owner_is_buyer')}; "
                 f"buyer probabilities by company {party.get('p_buyer_by_company')}")
    lines += ["", "For each flagged cell give verdict (correct / wrong / unsure), the right value for this project row,"
              " an exact quote from the document, and a short reason. Name the buyer with a quote.", "",
              "Document:", "---", text, "---"]
    return "\n".join(lines)


def tokens(text: str) -> int:
    return len(text) // 4


def estimate(run_dir: Path) -> dict:
    flags = flagged(run_dir)
    total_in = 0
    for key, f in flags.items():
        project, text = document(run_dir, key)
        total_in += tokens(RULES) + tokens(prompt(project, f, text))
    return {"deals": len(flags), "cells": sum(len(f["cells"]) for f in flags.values()), "input_tokens": total_in}


# --- code checks the model ---------------------------------------------------------------------------------------

def squash(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[‘’“”]", "'", s or "")).strip().lower()


def quote_found(quote: str, text: str) -> bool:
    """The quote is in the document (spacing and quote marks ignored; a quote cut with '...' is checked piece by piece)."""
    pieces = [p for p in re.split(r"\s*(?:\.\.\.|…)\s*", squash(quote)) if len(p) >= 8]
    return bool(pieces) and all(p in squash(text) for p in pieces)


def value_supported(value: float | None, quote: str, field: str, project_count: int = 1) -> bool:
    """The value is written in the quote as the field's kind of number, or follows from the quote's numbers by the
    validator's own rules (sum of nearby amounts, split by the row's project count or a stated count, metres x 250).
    The same rules the pipeline uses, so "4,000m of drilling" is metres and not millions (critic round 2, N11)."""
    if value is None:
        return True
    percent, kind = field in validate.PERCENT_FIELDS, validate.FIELD_KIND.get(field)
    if validate.find_number(quote, value, percent=percent, kind=kind):
        return True
    if percent:
        return False
    if field == "exp_comit_value":
        metres = value / validate.DRILLING_USD_PER_METRE
        if metres == int(metres) and re.search(r"(?<![\d.,])" + re.escape(f"{int(metres):,}").replace(",", ",?")
                                               + r"\s*(?:m\b|metres|meters)", quote):
            return True
    amounts = validate.written_amounts_at(quote, only=validate.SHARES) if kind == validate.SHARES else \
        validate.written_amounts_at(quote, exclude=validate.SHARES)
    return bool(validate.explain_by_rules(value, amounts, quote, kind, project_count))


def check_answer(answer: dict, text: str, project_count: int = 1) -> dict:
    for cell in answer.get("cells", []):
        cell["quote_checked"] = quote_found(cell["quote"], text) and value_supported(
            to_number(cell["value"]), cell["quote"], cell["field"], project_count)
    buyer = answer.get("buyer") or {}
    # the buyer's own name must be in its quote: a true sentence that names someone else proves nothing (N10)
    named = bool(buyer.get("name")) and set(company_words(buyer["name"])) <= set(squash(buyer.get("quote", "")).replace(",", " ").split())
    buyer["quote_checked"] = named and quote_found(buyer.get("quote", ""), text)
    return answer


# --- run ---------------------------------------------------------------------------------------------------------

def claude_code(system: str, user: str, schema: dict, run=subprocess.run) -> tuple[dict, dict]:
    """One `claude -p` call on the Claude subscription: no tools, no saved session, the answer forced to `schema`.
    Refuses to run when ANTHROPIC_API_KEY is set, because Claude Code would then bill that key instead."""
    if os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is set: claude -p would bill the API key, not the subscription")
    command = ["claude", "-p", "--model", MODEL, "--tools", "", "--no-session-persistence", "--output-format", "json",
               "--json-schema", json.dumps(schema), "--system-prompt", system]
    done = run(command, input=user, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    if done.returncode != 0:
        raise RuntimeError(f"claude -p failed ({done.returncode}): {done.stderr[:300]}")
    reply = json.loads(done.stdout)
    answer = reply.get("structured_output") or json.loads(reply["result"])
    usage = reply.get("usage") or {}
    return answer, {"input_tokens": usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
                    + usage.get("cache_creation_input_tokens", 0), "output_tokens": usage.get("output_tokens", 0),
                    "api_equivalent_usd": reply.get("total_cost_usd"), "model": MODEL}


def ask_model(project: str, flags: dict, text: str, call=claude_code) -> tuple[dict, dict]:
    user = prompt(project, flags, text)
    key = hashlib.sha256(json.dumps([MODEL, RULES, user, SCHEMA], sort_keys=True).encode()).hexdigest()[:32]
    path = CACHE / f"{key}.json"
    if path.exists():
        saved = json.loads(path.read_text())
        return saved["answer"], {"cached": True, **saved["usage"]}
    with telemetry.span(f"chat {MODEL}", **{"gen_ai.operation.name": "chat", "gen_ai.provider.name": "anthropic",
                                            "gen_ai.request.model": MODEL, "pipeline.llm.purpose": "escalate"}) as s:
        answer, usage = call(RULES, user, SCHEMA)
        s.set_attribute("gen_ai.usage.input_tokens", usage["input_tokens"])
        s.set_attribute("gen_ai.usage.output_tokens", usage["output_tokens"])
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"answer": answer, "usage": usage}, indent=1))
    return answer, {"cached": False, **usage}


PARALLEL = int(os.getenv("ESCALATE_PARALLEL", "13"))   # deals reviewed at once: independent claude -p processes


def run(run_dir: Path, call=claude_code) -> list[dict]:
    from concurrent.futures import ThreadPoolExecutor

    def one(item):
        key, flags = item
        project, text = document(run_dir, key)
        answer, usage = ask_model(project, flags, text, call)
        count = max((c.get("project_count") or 1) for c in flags["cells"]) if flags["cells"] else 1
        return {"row_id": key[0], "project": project, "usage": usage, **check_answer(answer, text, count)}

    with ThreadPoolExecutor(PARALLEL) as pool:      # results keep the order of the flagged deals
        return list(pool.map(one, flagged(run_dir).items()))


def summary(results: list[dict]) -> str:
    new = [r["usage"] for r in results if not r["usage"]["cached"]]
    tokens_in, tokens_out = sum(u["input_tokens"] for u in new), sum(u["output_tokens"] for u in new)
    cells = [(r["row_id"], c) for r in results for c in r.get("cells", [])]
    out = ["", f"## Escalation to Claude `{MODEL}` on the subscription (only deals the cheap tiers could not settle)\n",
           f"- Deals escalated: {len(results)}; cells: {len(cells)}; verdicts: "
           f"{ {v: sum(c['verdict'] == v for _, c in cells) for v in ('correct', 'wrong', 'unsure')} }",
           f"- Answers whose quote and value code could confirm: {sum(c['quote_checked'] for _, c in cells)} of {len(cells)}",
           f"- Calls: {len(new)} new, {len(results) - len(new)} cached; {tokens_in:,} input and {tokens_out:,} output tokens "
           "of subscription usage; no API charge", "",
           "| Row | Field | Verdict | Value | Quote checked | Reason |", "|---|---|---|---|---|---|"]
    for row_id, c in cells:
        value = "-" if c["value"] is None else f"{c['value']:,.10g}"
        out.append(f"| {row_id} | `{c['field']}` | {c['verdict']} | {value} | {c['quote_checked']} | {c['reason'][:110]} |")
    out += ["", "| Row | Buyer | Quote checked | Reason |", "|---|---|---|---|"]
    out += [f"| {r['row_id']} | {r['buyer'].get('name')} | {r['buyer']['quote_checked']} | {r['buyer'].get('reason', '')[:110]} |"
            for r in results]
    return "\n".join(out) + "\n"


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("estimate", "run"))
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args(argv)
    if args.command == "estimate":
        e = estimate(args.run_dir)
        print(f"{e['deals']} deals, {e['cells']} cells, about {e['input_tokens']:,} input tokens to Claude {MODEL} "
              "on the subscription (one claude -p call per deal; no API charge)")
        return
    telemetry.setup()
    try:
        results = run(args.run_dir)
    finally:
        telemetry.shutdown()
    (args.run_dir / "escalations.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in results))
    text = summary(results)
    summary_path = args.run_dir / "summary.md"
    summary_path.write_text(summary_path.read_text().split("\n## Escalation")[0].rstrip("\n") + "\n" + text)
    print(text)


if __name__ == "__main__":
    main()
