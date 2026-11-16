"""The critic's yes/no check: for every value of a sample run, is it correctly taken from the document?

    python -m evals.jev_verify RUN_DIR          # RUN_DIR written by `python -m evals.end_to_end run`

Code and Jev split the work. The validator already decided, with code, whether each number is written in the
document (FOUND) or follows from written amounts by a rule (DERIVED: sums, splits, metres x 250, shares x price).
What code cannot tell is whether that number MEANS what the field says: a share count is not a cash payment,
a placement is not a deal payment, an older deal's price is not this deal's. That is a closed question, so it
goes to Jev, with the passage around the number and the field's definition, examples and counterexamples from
the extraction rules (docs/extraction-rules/) written into the criteria. Jev never does the arithmetic.

Checked for each entry:
  pipeline   every FOUND or DERIVED value of the final extraction (the values that reach the database)
  analyst    the analyst's value, when it differs from the pipeline's and is written in the document,
             so a disagreement shows which side the document supports

Writes RUN_DIR/jev_verdicts.jsonl (one line per value: Jev's probability of "correct", the role it gives the
number, the passage it read) and appends a section to RUN_DIR/summary.md, including how often Jev's verdict
matches the analyst comparison, which is the only calibration this check has.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from evals import end_to_end, jev
from pipeline import telemetry
from pipeline.pdf import pdf_text
from pipeline.search import company_words
from pipeline.validate import DERIVED, FIELD_KIND, FOUND, PERCENT_FIELDS, find_number, to_number

THRESHOLD = 0.5           # noul below this = Jev says the value is not correctly extracted
SIDE_CHARS = 700          # passage: this much text on each side of the number, cut at sentence ends
MAX_PASSAGE = 3000

ROLES = {
    "cash_payment": "Cash the buyer pays the vendor for the project in this transaction: upfront, deferred, option or "
                    "milestone payments, or cash paid to receive shares in the vendor.",
    "exploration_commitment": "Money (or metres of drilling) the buyer commits to spend on exploring the project to earn "
                              "or keep its interest.",
    "consideration_shares": "Number of the buyer's own shares issued to the vendor as payment for the project.",
    "share_price": "Price per share at which the consideration shares are issued or valued.",
    "shares_value": "Money value of the consideration shares.",
    "royalty": "Royalty percentage (NSR or gross revenue royalty) created, retained or acquired in this transaction.",
    "interest": "Percentage interest in the project acquired or that can be earned in this transaction.",
    "area": "Surface area of the project.",
    "drilling": "Metres of drilling committed.",
    "total_consideration": "Total value of the transaction: cash plus exploration commitment plus value of shares.",
    "financing": "Money raised from investors (placement, capital raising, convertible note), not paid for the project.",
    "other_deal_or_project": "A term of an earlier, legacy or superseded agreement, or of a different project.",
    "other": "A resource estimate, assay result, market capitalisation, share count on issue, date or other number.",
}
FIELD_ROLE = {"cash_payments_value": "cash_payment", "exp_comit_value": "exploration_commitment",
              "shares_amount": "consideration_shares", "share_price": "share_price", "shares_value": "shares_value",
              "nsr_percent": "royalty", "interest_acquired_percent": "interest", "project_area": "area",
              "drilling_amount": "drilling", "aggregate_deal_value": "total_consideration"}

# Definition, what makes it correct, what makes it wrong. From the prompt schema and docs/extraction-rules.
FIELDS = {
    "cash_payments_value": (
        "the total cash the buyer pays for the project in this transaction",
        "The passage states this cash amount as payment for this project in the transaction being announced; or the "
        "value is the sum of its cash tranches (upfront, deferred, option and milestone payments, including conditional "
        "ones); or it is a cash total shared by several projects divided equally among them; or cash the buyer pays to "
        "receive shares in the vendor.",
        "The amount is a number of shares, or a value of shares; or money raised in a placement or financing; or "
        "exploration spending; or the price to buy back part of a royalty; or a payment of an earlier, legacy or "
        "superseded agreement; or a payment for a different project; or only some of the cash tranches."),
    "exp_comit_value": (
        "the exploration expenditure the buyer commits to for this project",
        "The passage states this amount as exploration expenditure the buyer must spend on this project to earn or keep "
        "its interest; or a commitment shared by several projects divided equally; or a commitment in metres of drilling "
        "converted at 250 USD per metre.",
        "The amount is past exploration spending; or a cash payment to the vendor; or money raised in a financing; or a "
        "commitment for a different project; or the total of a commitment that is shared by several projects."),
    "shares_amount": (
        "the number of the buyer's shares issued to the vendor as consideration",
        "The passage states this number of the buyer's shares (or the sum of its tranches) issued to the vendor as "
        "payment for this project.",
        "The number is shares issued in a placement or capital raising; or the shares on issue; or options or warrants "
        "not paid as consideration; or shares for a different deal; or a money amount."),
    # The client defines share price as market data: "idealmente valor de la fecha de la noticia, si muy complicado
    # del dia de scrap" (field sheet, 5 Sep 2025). So a price not written in the announcement can still be right.
    "share_price": (
        "the price per share of the buyer's shares used to value the consideration shares",
        "The passage states this price as the issue price or deemed price of the shares issued for this project, or "
        "as the buyer's market share price on or near the announcement date.",
        "The price is a placement price for raising money; or an old or unrelated trading price; or the price of a "
        "different company's shares; or a money amount that is not per share."),
    "shares_value": (
        "the value of the consideration shares",
        "The passage states this as the value of the shares issued for this project, or it is the number of those "
        "shares times their stated issue price.",
        "The value is a financing amount; or a cash payment; or a value for a different deal."),
    "nsr_percent": (
        "the royalty percentage in this transaction",
        "The passage states this royalty (net smelter royalty or gross revenue royalty) as acquired by the buyer or "
        "retained by the vendor in this transaction.",
        "The percentage is the part of a royalty that can be bought back; or an interest percentage; or a royalty on a "
        "different project or from an earlier agreement."),
    "interest_acquired_percent": (
        "the percentage interest in the project the buyer acquires",
        "The passage states this as the interest the buyer acquires in this project, or, for an earn-in or farm-in, the "
        "final interest the buyer can earn.",
        "The percentage is an intermediate stage when a higher final interest is stated; or the vendor's retained "
        "interest; or a royalty; or a shareholding in a company; or an interest in a different project."),
    "project_area": (
        "the surface area of the project",
        "The passage states this as the area of this project, in any unit.",
        "The area is the total of several projects when this project's own area is given; or the area of a different "
        "project; or a length such as metres of drilling."),
    "aggregate_deal_value": (
        "the total consideration of the transaction",
        "The passage supports this as the total of cash payments, exploration commitments and the value of shares for "
        "this transaction.",
        "The value is an enterprise value or market capitalisation; or a financing amount; or counts a combined cash "
        "and shares figure twice."),
    "drilling_amount": (
        "the metres of drilling the buyer commits to",
        "The passage states this many metres of drilling committed by the buyer on this project.",
        "The number is metres already drilled, a drill intercept, or drilling on a different project."),
}


# --- who and what: buyer or seller, ticker, project, deal type ------------------------------------------------
# These are judgments, not numbers, so no code checks them: the validator only checks numbers. The prompt asks the
# model to put the BUYER in owner_name; nothing verified that it did until this check.

OPENING_CHARS = 1200      # the opening paragraph usually says who acquires what
_DEAL_TERMS = re.compile(r"acqui|purchas|\bsells?\b|\bsale\b|\bsold\b|vendor|option|earn[- ]?in|farm[- ]?(?:in|out)|"
                         r"joint venture|\bjv\b|merger|takeover|consideration|agreement|transfer|divest", re.I)
BUYER = jev.noul(
    "Is named_company the buyer in this transaction: the company that acquires, or can earn, the interest in the project?",
    yes="named_company acquires, purchases, earns into, farms into or takes an option over the project interest, or pays "
        "the consideration for it (cash, its own shares, exploration spending); in a merger or takeover it is the "
        "company that acquires the other.",
    no="named_company is the vendor or seller: it sells, divests, grants the option, farms out, or receives the "
       "payments or shares; or it is a third party (adviser, government, a partner that acquires nothing); or it is "
       "not in the passage.")
ROLE = jev.choice("What is named_company's role in the transaction in document_passage?", {
    "buyer": "Acquires or earns the interest in the project and pays the consideration.",
    "seller": "Sells, options out or farms out the project and receives the consideration.",
    "other_party": "Takes part in some other way: adviser, government, partner that acquires nothing.",
    "not_mentioned": "Does not appear in the passage."})
TICKER = jev.noul(
    "Is ticker the stock ticker of the buyer in this transaction (the company that acquires or earns the interest)?",
    yes="The passage gives ticker for the company that acquires the interest, for example 'Acme Gold Ltd (ASX:ACM) "
        "will acquire'.",
    no="ticker belongs to the vendor or seller or to another company, or the passage does not connect it to the buyer.")
PROJECT = jev.noul(
    "Is project the project, property, tenement or claims that is the object of this transaction?",
    yes="The transaction acquires, sells, options, farms into or joint-ventures this project or its tenements or claims.",
    no="The project is only mentioned as nearby, or is already owned and not part of the transaction, or the "
       "transaction is over a different project.")
DEAL_TYPE = jev.noul(
    "Does deal_type correctly describe the transaction in document_passage?",
    yes="deal_type names the kind of transaction the passage describes (for example acquisition, sale, option, "
        "earn-in, farm-in, joint venture, merger), allowing other wording for the same thing.",
    no="deal_type names a different kind of transaction, or the passage describes no transaction of that kind.")


def deal_passage(text: str, company: str) -> str:
    """The opening of the announcement, plus the sentences that name the company together with a deal term."""
    opening = text[:OPENING_CHARS]
    stop = opening.rfind(". ")
    opening = opening[:stop + 1] if stop > 0 else opening
    words = company_words(company)
    picked = []
    for sentence in re.split(r"(?<=[.!?])\s+", text[len(opening):]):
        low = sentence.lower()
        if words and any(w in low for w in words) and _DEAL_TERMS.search(sentence) and len(sentence) < 600:
            picked.append(sentence)
        if len(picked) == 4:
            break
    return (opening + (" [...] " + " [...] ".join(picked) if picked else ""))[:MAX_PASSAGE]


def is_same_company(a: str | None, b: str | None) -> bool:
    return bool(a and b) and bool(set(company_words(a)) & set(company_words(b)))


def check_parties(case: dict, entry: dict, row: dict, text: str) -> list[dict]:
    """One Jev call for the pipeline's who-and-what fields, and one for the analyst's owner when it differs."""
    extraction = entry.get("extraction") or {}
    owner, ticker = extraction.get("owner_name"), extraction.get("owner_ticker")
    project, deal_type = entry.get("project_name") or extraction.get("project_name"), extraction.get("deal_type")
    base = {"case_id": case["id"], "row_id": entry["id"]}
    out = []
    if owner:
        state = {"named_company": owner, "project": project, "document_passage": deal_passage(text, owner)}
        questions = {"named_company_is_buyer": BUYER, "role": ROLE, "project_is_object": PROJECT}
        if ticker:
            state["ticker"], questions["ticker_is_buyers"] = ticker, TICKER
        if deal_type:
            state["deal_type"], questions["deal_type_fits"] = deal_type, DEAL_TYPE
        a = jev.decide(state, questions)
        out.append({**base, "side": "pipeline", "check": "parties", "owner_name": owner, "owner_ticker": ticker,
                    "project_name": project, "deal_type": deal_type,
                    "p_buyer": a["named_company_is_buyer"]["noul"], "role": a["role"]["choice"],
                    "role_probabilities": a["role"].get("probabilities"),
                    "p_project_is_object": a["project_is_object"]["noul"],
                    "p_ticker_is_buyers": a.get("ticker_is_buyers", {}).get("noul"),
                    "p_deal_type_fits": a.get("deal_type_fits", {}).get("noul"),
                    "analyst_owner": row.get("owner_name"), "same_owner_as_analyst": is_same_company(owner, row.get("owner_name")),
                    "passage": state["document_passage"]})
    if row.get("owner_name") and not is_same_company(owner, row["owner_name"]):
        state = {"named_company": row["owner_name"], "project": project or row.get("project_name"),
                 "document_passage": deal_passage(text, row["owner_name"])}
        a = jev.decide(state, {"named_company_is_buyer": BUYER, "role": ROLE})
        out.append({**base, "side": "analyst", "check": "parties", "owner_name": row["owner_name"],
                    "p_buyer": a["named_company_is_buyer"]["noul"], "role": a["role"]["choice"],
                    "role_probabilities": a["role"].get("probabilities"), "pipeline_owner": owner,
                    "passage": state["document_passage"]})
    return out


def sentence_window(text: str, start: int, end: int) -> str:
    """The text from about SIDE_CHARS before `start` to SIDE_CHARS after `end`, cut at sentence ends."""
    left = max(0, start - SIDE_CHARS)
    stop = text.rfind(". ", left, start)
    left = stop + 2 if stop >= 0 else left
    right = min(len(text), end + SIDE_CHARS)
    stop = text.find(". ", end, right)
    right = stop + 1 if stop >= 0 else right
    return text[left:right]


def passage(text: str, field: str, value: float, evidence: str | None, status: str) -> str | None:
    """The part of the document a reader needs to judge this value: around the value itself, or for a derived
    value around each written amount the rule used."""
    windows = []
    targets = [value] if status == FOUND else [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", evidence or "")]
    for target in targets:
        snippet = find_number(text, target, percent=field in PERCENT_FIELDS, kind=FIELD_KIND.get(field))
        at = text.find(snippet) if snippet else -1
        if at >= 0:
            window = sentence_window(text, at, at + len(snippet))
            if not any(window in w for w in windows):
                windows.append(window)
    joined = " [...] ".join(windows)
    return joined[:MAX_PASSAGE] or None


def ask(project: str, field: str, value: float, unit: str | None, how: str, text_passage: str) -> dict:
    definition, yes, no = FIELDS[field]
    state = {"project": project, "field": definition, "extracted_value": f"{value:,.10g}" + (f" {unit}" if unit else ""),
             "how_the_value_follows_from_the_document": how, "document_passage": text_passage}
    questions = {
        "correct": jev.noul(f"Is extracted_value correctly {definition} for the project named in state, according to "
                            f"document_passage? Any arithmetic in how_the_value_follows_from_the_document was already "
                            f"checked; judge only what the numbers mean.", yes=yes, no=no),
        "role": jev.choice("In document_passage, what is the number (or the numbers it was computed from) that "
                           "extracted_value comes from?", ROLES),
    }
    answers = jev.decide(state, questions)
    return {"p_correct": answers["correct"]["noul"], "role": answers["role"]["choice"],
            "role_confidence": answers["role"].get("confidence"), "role_probabilities": answers["role"].get("probabilities")}


def verify(run_dir: Path, cases_path: Path = end_to_end.DEFAULT_CASES, rows_path: Path = end_to_end.DEFAULT_ROWS) -> list[dict]:
    sample = json.loads((run_dir / "sample.json").read_text())
    # The case records saved with the run; older runs saved only ids, so fall back to the case file by row id.
    records = sample.get("case_records") or [c for c in end_to_end.load_cases(cases_path)
                                             if c["row_id"] in {int(i.rsplit("-", 1)[1]) for i in sample["cases"]}]
    by_row = {c["row_id"]: c for c in records}
    rows = end_to_end.load_rows(rows_path)
    report = json.loads((run_dir / "run_report.json").read_text())
    comparison = [json.loads(line) for line in (run_dir / "comparison.jsonl").read_text().splitlines()]
    verdicts = []
    for entry in report:
        validation = entry.get("validation")
        if not validation:
            continue
        case = by_row[entry["id"]]
        # The document the pipeline accepted, read the same way (search.py joins the text on single spaces).
        raw, _ = pdf_text(Path(entry["source_url"].removeprefix("file://")).read_bytes())
        text = " ".join(raw.split())
        verdicts += check_parties(case, entry, rows[entry["id"]], text)
        project = entry.get("project_name") or case.get("project_name") or ""
        extraction = entry.get("extraction") or {}
        for field, check in validation["fields"].items():
            if field not in FIELDS or check["status"] not in (FOUND, DERIVED) or field in (entry.get("withheld") or {}):
                continue
            text_passage = passage(text, field, check["value"], check["evidence"], check["status"])
            if not text_passage:
                continue
            how = "written in the document" if check["status"] == FOUND else f"derived by code: {check['evidence']}"
            unit = extraction.get("project_area_unit") if field == "project_area" else extraction.get(end_to_end.CURRENCIES.get(field, ""))
            verdicts.append({"case_id": case["id"], "row_id": entry["id"], "side": "pipeline", "check": "value", "field": field,
                             "value": check["value"], "status": check["status"], "evidence": check["evidence"],
                             **ask(project, field, check["value"], unit, how, text_passage), "passage": text_passage})
        for line in comparison:
            if line["row_id"] != entry["id"] or line["verdict"] not in ("disagree", "analyst_only") or line["field"] not in FIELDS:
                continue
            analyst = to_number(line["analyst"])
            snippet = analyst and find_number(text, analyst, percent=line["field"] in PERCENT_FIELDS, kind=FIELD_KIND.get(line["field"]))
            if not snippet:
                continue
            text_passage = passage(text, line["field"], analyst, None, FOUND)
            verdicts.append({"case_id": case["id"], "row_id": entry["id"], "side": "analyst", "check": "value", "field": line["field"],
                             "value": analyst, "status": FOUND, "evidence": snippet,
                             **ask(project, line["field"], analyst, line.get("analyst_currency"), "written in the document", text_passage),
                             "passage": text_passage})
    for v in verdicts:
        if v["check"] == "value":
            v["jev_says"] = "correct" if v["p_correct"] >= THRESHOLD else "wrong"
            v["role_matches_field"] = v["role"] == FIELD_ROLE[v["field"]]
        else:
            low = {name: v.get(f"p_{name}") for name in ("buyer", "project_is_object", "ticker_is_buyers", "deal_type_fits")}
            v["flags"] = [name for name, p in low.items() if p is not None and p < THRESHOLD]
            if v["role"] != "buyer" and "buyer" not in v["flags"]:
                v["flags"].append(f"role {v['role']}")
    return verdicts


def parties_summary(verdicts: list[dict]) -> list[str]:
    pipeline = [v for v in verdicts if v["check"] == "parties" and v["side"] == "pipeline"]
    analyst = [v for v in verdicts if v["check"] == "parties" and v["side"] == "analyst"]
    flagged = [v for v in pipeline if v["flags"]]
    return ["", "### Who and what: buyer or seller, ticker, project, deal type\n",
            f"- Stored deals checked: {len(pipeline)}; owner_name is the buyer (p >= {THRESHOLD}): "
            f"{sum(v['p_buyer'] >= THRESHOLD for v in pipeline)}; Jev's role for it: "
            f"{dict(Counter(v['role'] for v in pipeline))}",
            f"- Same owner as the analyst: {sum(v['same_owner_as_analyst'] for v in pipeline)} of {len(pipeline)}",
            f"- Where the owners differ, the analyst's owner is the buyer (p >= {THRESHOLD}): "
            f"{sum(v['p_buyer'] >= THRESHOLD for v in analyst)} of {len(analyst)}",
            "", "| Case | owner_name | P(buyer) | Role | P(project is the object) | P(ticker is buyer's) | P(deal type fits) | Analyst's owner (P buyer) |",
            "|---|---|---|---|---|---|---|---|",
            *[f"| {v['case_id']} | {v['owner_name']} | {p(v['p_buyer'])} | {v['role']} | {p(v['p_project_is_object'])} | "
              f"{p(v['p_ticker_is_buyers'])} | {p(v['p_deal_type_fits'])} | "
              + next((f"{a['owner_name']} ({p(a['p_buyer'])})" for a in analyst if a["row_id"] == v["row_id"]), "same")
              + " |" for v in flagged]]


def p(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def summary(verdicts: list[dict], comparison: list[dict]) -> str:
    agreement = {(c["row_id"], c["field"]): c["verdict"] for c in comparison}
    verdicts_all, verdicts = verdicts, [v for v in verdicts if v["check"] == "value"]
    pipeline = [v for v in verdicts if v["side"] == "pipeline"]
    calibration = Counter((agreement.get((v["row_id"], v["field"]), "no analyst value"), v["jev_says"]) for v in pipeline)
    flagged = [v for v in pipeline if v["jev_says"] == "wrong" or not v["role_matches_field"]]
    analyst = [v for v in verdicts if v["side"] == "analyst"]
    out = ["", f"## Jev check of the extracted values (`{jev.MODEL}`, threshold {THRESHOLD})\n",
           f"- Pipeline values checked: {len(pipeline)}; Jev says correct: {sum(v['jev_says'] == 'correct' for v in pipeline)}; "
           f"flagged (says wrong, or gives the number another role): {len(flagged)}",
           "- Against the analyst comparison (the only calibration):",
           *[f"  - analyst {k[0]}, Jev {k[1]}: {n}" for k, n in sorted(calibration.items())],
           f"- Analyst values that differ from the pipeline and are written in the document: {len(analyst)}; "
           f"Jev says the analyst's value is correct: {sum(v['jev_says'] == 'correct' for v in analyst)}",
           f"- Jev calls: {jev.spend['calls']} paid, {jev.spend['cached']} cached, {jev.spend['usd']:.5f} USD", "",
           "### Flagged pipeline values\n", "| Case | Field | Value | Status | P(correct) | Role Jev gives it |", "|---|---|---|---|---|---|",
           *[f"| {v['case_id']} | `{v['field']}` | {v['value']:,.10g} | {v['status']} | {v['p_correct']:.2f} | {v['role']} |" for v in flagged],
           *parties_summary(verdicts_all)]
    return "\n".join(out) + "\n"


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--cases", type=Path, default=end_to_end.DEFAULT_CASES, help="only for runs saved without case records")
    parser.add_argument("--rows", type=Path, default=end_to_end.DEFAULT_ROWS)
    args = parser.parse_args(argv)
    telemetry.setup()
    try:
        verdicts = verify(args.run_dir, args.cases, args.rows)
    finally:
        telemetry.shutdown()
    (args.run_dir / "jev_verdicts.jsonl").write_text("".join(json.dumps(v, ensure_ascii=False) + "\n" for v in verdicts))
    comparison = [json.loads(line) for line in (args.run_dir / "comparison.jsonl").read_text().splitlines()]
    text = summary(verdicts, comparison)
    summary_path = args.run_dir / "summary.md"
    before = summary_path.read_text().split("\n## Jev check")[0].rstrip("\n") + "\n"   # a re-run replaces its section
    summary_path.write_text(before + text)
    print(text)


if __name__ == "__main__":
    main()
