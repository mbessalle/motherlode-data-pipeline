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

from evals import cell_check
from pipeline import telemetry
from pipeline.pdf import pdf_text
from pipeline.validate import to_number

MODEL = os.getenv("ESCALATE_MODEL", "opus")          # a Claude Code model alias, run on the subscription
MAX_DOCUMENT_CHARS = 60_000        # about 15,000 tokens; a longer document is cut, and the cut is stated
TIMEOUT_SECONDS = 600
CACHE = Path(__file__).parent / ".cache" / "escalate"

RULES = """You check a mining deal database row against its source announcement. The client's rules (Motherlode
Consulting field sheet and emails):
- owner_name, owner_ticker: the BUYER, the company that acquires or can earn the interest ("empresa que compra").
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
Be rigorous: read the whole document, quote the exact words that support each value, and say "unsure" rather
than guess."""

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["cells", "buyer"], "properties": {
    "cells": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                         "required": ["field", "verdict", "value", "quote", "reason"],
                                         "properties": {"field": {"type": "string"},
                                                        "verdict": {"type": "string", "enum": ["correct", "wrong", "unsure"]},
                                                        "value": {"type": ["number", "null"]},
                                                        "quote": {"type": "string"}, "reason": {"type": "string"}}}},
    "buyer": {"type": "object", "additionalProperties": False, "required": ["name", "quote", "reason"],
              "properties": {"name": {"type": ["string", "null"]}, "quote": {"type": "string"}, "reason": {"type": "string"}}}}}


def flagged(run_dir: Path) -> dict[int, dict]:
    """Deals with at least one cell not accepted, or a buyer not accepted: {row_id: {'cells': [...], 'party': {...}}}."""
    out: dict[int, dict] = {}
    for line in (run_dir / "cells.jsonl").read_text().splitlines():
        cell = json.loads(line)
        if cell["action"] != "accept":
            out.setdefault(cell["row_id"], {"cells": [], "party": None})["cells"].append(cell)
    for line in (run_dir / "parties.jsonl").read_text().splitlines():
        party = json.loads(line)
        if party.get("action") in ("review", "correct"):
            out.setdefault(party["row_id"], {"cells": [], "party": None})["party"] = party
        elif party["row_id"] in out:
            out[party["row_id"]]["party"] = party
    return out


def document(run_dir: Path, row_id: int) -> tuple[str, str]:
    """(project name, document text as the pipeline read it, cut at MAX_DOCUMENT_CHARS with a note)."""
    entry = next(e for e in json.loads((run_dir / "run_report.json").read_text()) if e["id"] == row_id)
    raw, _ = pdf_text(Path(entry["source_url"].removeprefix("file://")).read_bytes())
    text = " ".join(raw.split())
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
    for row_id, f in flags.items():
        project, text = document(run_dir, row_id)
        total_in += tokens(RULES) + tokens(prompt(project, f, text))
    return {"deals": len(flags), "cells": sum(len(f["cells"]) for f in flags.values()), "input_tokens": total_in}


# --- code checks the model ---------------------------------------------------------------------------------------

def squash(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[‘’“”]", "'", s or "")).strip().lower()


def quote_found(quote: str, text: str) -> bool:
    """The quote is in the document (spacing and quote marks ignored; a quote cut with '...' is checked piece by piece)."""
    pieces = [p for p in re.split(r"\s*(?:\.\.\.|…)\s*", squash(quote)) if len(p) >= 8]
    return bool(pieces) and all(p in squash(text) for p in pieces)


def value_supported(value: float | None, quote: str, field: str) -> bool:
    """The value is written in the quote, or follows from the quote's numbers by a rule (sum, split by 2-6, x250)."""
    if value is None:
        return True
    if cell_check.number_spans(quote, value, percent=field in cell_check.PERCENT_FIELDS):
        return True
    numbers = [float(n.replace(",", "")) * (1e6 if (u or "").lower() in ("m", "million") else 1)
               for n, u in re.findall(r"(\d[\d,]*(?:\.\d+)?)\s*(m\b|million)?", quote, re.I)]
    numbers = [n for n in numbers if n]
    candidates = set(numbers) | {sum(numbers)} | {n * 250 for n in numbers} | {n / k for n in numbers for k in range(2, 7)}
    candidates |= {(a + b) / k for i, a in enumerate(numbers) for b in numbers[i + 1:] for k in range(1, 7)}
    return any(abs(c - value) <= max(1.0, 0.01 * abs(value)) for c in candidates)


def check_answer(answer: dict, text: str) -> dict:
    for cell in answer.get("cells", []):
        cell["quote_checked"] = quote_found(cell["quote"], text) and value_supported(to_number(cell["value"]), cell["quote"], cell["field"])
    buyer = answer.get("buyer") or {}
    buyer["quote_checked"] = bool(buyer.get("name")) and quote_found(buyer.get("quote", ""), text)
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


def run(run_dir: Path, call=claude_code) -> list[dict]:
    results = []
    for row_id, flags in flagged(run_dir).items():
        project, text = document(run_dir, row_id)
        answer, usage = ask_model(project, flags, text, call)
        results.append({"row_id": row_id, "project": project, "usage": usage, **check_answer(answer, text)})
    return results


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
