"""Check every cell of every stored deal against its document: code finds, Jev judges, and proposes corrections.

    python -m evals.cell_check RUN_DIR       # RUN_DIR written by `python -m evals.end_to_end run`

Code and Jev split the work by what each does reliably:

  code   finds every place a value is written (all its forms: "$1.1m", "1,100,000", "1.1 million"), does all
         arithmetic (sums, splits, metres x 250, shares x price, km2 to ha), reads the market price
  Jev    judges meaning, with probabilities: does this passage state this value as this field of this deal?
         Which of these numbers is this field? Which of these companies is the buyer?

For each numeric cell the result says where the value comes from (its `source`):
  stated          written in the document; Jev judges each place it is written and the best passage counts
  computed        follows from written parts by a rule (the validator's evidence); Jev judges the parts' passages
  market          a share price not in the document, compared with the buyer's close on the news date (pipeline/market.py)
  not_in_document none of the above
and what to do with it (`action`): accept (P >= ACCEPT), correct (a better value proposed with P >= ACCEPT),
or review. A cell that is not accepted gets a candidate pick: code collects the numbers of the right kind from the
passages about that field, and Jev picks which one is the field (or none), with probabilities.

For the parties: every company named in the deal passage gets a probability of being the buyer (the client's
owner is the company buying the asset), and the ticker, project and deal type each get a probability of being right.

Writes RUN_DIR/cells.jsonl (one line per cell) and RUN_DIR/parties.jsonl, and a section of RUN_DIR/summary.md
with the counts and the agreement with the analyst's table, leaving out the rows the critic found to be wrong
in that table (KNOWN_DATA_PROBLEMS).
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from evals import end_to_end, jev
from evals.jev_verify import DEAL_TYPE, FIELDS, PROJECT, TICKER, ask, passage, sentence_window
from pipeline import market, telemetry
from pipeline.buyer import BUYER, ROLE, party_passage
from pipeline.pdf import pdf_text
from pipeline.search import company_words
from pipeline.validate import (
    _CURRENCY_BEFORE,
    DERIVED,
    DRILLING_USD_PER_METRE,
    FIELD_KIND,
    MONEY,
    PERCENT_FIELDS,
    SHARES,
    _forms,
    number_in_words,
    occurrence_kind,
    to_number,
)

ACCEPT, REVIEW = 0.8, 0.5      # starting thresholds; to be set from the calibration below
MAX_PLACES = 4                 # places a value is written that Jev judges, the most deal-like first
MAX_CANDIDATES = 8
MAX_COMPANIES = 6
PRICE_TOLERANCE = 0.10         # a stored price within 10% of the market close counts as the market price

# Rows of the seed-7 sample where the critic found the analyst's value itself wrong (critic report, 16 Nov 2026).
KNOWN_DATA_PROBLEMS = {(357, "owner_name"), (401, "owner_name"), (347, "owner_name"), (308, "share_price"),
                       (309, "share_price"), (347, "share_price"), (308, "shares_amount"), (349, "exp_comit_value")}

FIELD_WORDS = {
    "cash_payments_value": r"cash|pay|payment|consideration|upfront|deferred|milestone|option fee",
    "exp_comit_value": r"expenditure|spend|exploration|commit|earn|drill",
    "shares_amount": r"shares|issue|consideration|tranche",
    "share_price": r"per share|issue price|deemed|price of|vwap|cents",
    "shares_value": r"shares|equity|scrip|consideration",
    "aggregate_deal_value": r"total|aggregate|consideration|valu",
    "nsr_percent": r"royalty|nsr|net smelter|grr|gross revenue",
    "interest_acquired_percent": r"interest|earn|acquire|stake|ownership",
    "project_area": r"hectares|\bha\b|km2|km²|square kilomet|sq\.? ?km|area|covers|comprising",
    "drilling_amount": r"metres|meters|drill",
}
_NUM = r"(?<![\d.,])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_SCALE = r"(?:\s*(million|m\b|thousand|k\b))?"
_CURRENCY = r"(?:[A-Z]{0,2}\$|\b(?:CAD|USD|AUD|EUR|GBP|NZD)\s?)"
CANDIDATE = {
    "money": re.compile(_CURRENCY + r"\s?" + _NUM + _SCALE, re.I),
    "shares": re.compile(_NUM + r"(?:\s*(million))?\s+(?:[a-z-]+\s){0,3}(?:shares|ordinary shares)", re.I),
    # a price, not an amount: "$0.05" or "5 cents", never "$1.5m" or "$2.5 million"
    "price": re.compile(r"(?:" + _CURRENCY + r"\s?(\d*\.\d+)(?!\d|\s*(?:m\b|million|k\b|thousand|bn\b|billion))"
                        r"|(\d+(?:\.\d+)?)\s*cents?)", re.I),
    "percent": re.compile(_NUM + r"\s*(?:%|per\s?cent|percent)", re.I),
    "area": re.compile(_NUM + r"\s*(hectares?|ha\b|km2|km²|square kilomet\w*|sq\.? ?km)", re.I),
    "metres": re.compile(_NUM + r"\s*(?:metres|meters|m\b)\s+(?:of\s+)?(?:\w+\s){0,2}drill", re.I),
}
FIELD_CANDIDATES = {"cash_payments_value": "money", "exp_comit_value": "money", "shares_value": "money",
                    "aggregate_deal_value": "money", "shares_amount": "shares", "share_price": "price",
                    "nsr_percent": "percent", "interest_acquired_percent": "percent", "project_area": "area",
                    "drilling_amount": "metres"}
_COMPANY = re.compile(r"\b((?:[A-Z][A-Za-z0-9&'.-]*\s){0,4}(?:Limited|Ltd\.?|Corporation|Corp\.?|Inc\.?|NL|plc|PLC|"
                      r"Pty\.? Ltd\.?|Resources|Metals|Minerals|Mining|Gold|Energy|Holdings|Group))\b")


# --- code: find ---------------------------------------------------------------------------------------------------

def number_spans(text: str, x: float, percent: bool = False, kind: str | None = None) -> list[tuple[int, int]]:
    """Every place `x` is written. Like validate.find_number, but all places, and "1m" counts as a million only
    right after a currency sign: "1m @ 6.15 g/t" is a drill intercept (critic finding F1)."""
    patterns = []
    for form in _forms(x):
        tail = r"\s*(?:%|per\s?cent|percent)" if percent else r"(?![\d]|[.,]\d)"
        patterns.append((r"(?<![\d.,])" + re.escape(form) + r"(?:\.0+)?" + tail, False))
    if percent and x == int(x) and number_in_words(int(x)):
        patterns.append((r"(?<!ty[- ])\b" + number_in_words(int(x)) + r"\s*(?:per\s?cent|percent)", False))
    if not percent and x >= 1e5:
        patterns += [(r"(?<![\d.,])" + re.escape(f) + r"\s*(?:million|mm\b)", False) for f in _forms(x / 1e6)]
        patterns += [(r"(?<![\d.,])" + re.escape(f) + r"\s*m\b", True) for f in _forms(x / 1e6)]
        patterns += [(r"(?<![\d.,])" + re.escape(f) + r"\s*(?:thousand|k\b)", False) for f in _forms(x / 1e3)]
    other = {MONEY: SHARES, SHARES: MONEY}.get(kind)
    spans = set()
    for pattern, needs_currency in patterns:
        for match in re.finditer(pattern, text, re.I):
            if needs_currency and not _CURRENCY_BEFORE.search(text[max(0, match.start() - 5):match.start()]):
                continue
            if other and occurrence_kind(text, match.start(), match.end()) == other:
                continue
            spans.add((match.start(), match.end()))
    return sorted(spans)


def field_score(text: str, field: str) -> int:
    return len(re.findall(FIELD_WORDS[field], text, re.I))


def best_places(text: str, field: str, spans: list[tuple[int, int]]) -> list[str]:
    """The passages around the places a value is written, the ones that talk about this field first."""
    windows = []
    for start, end in spans:
        window = sentence_window(text, start, end)
        if window not in windows:
            windows.append(window)
    return sorted(windows, key=lambda w: -field_score(w, field))[:MAX_PLACES]


def field_passage(text: str, field: str, limit: int = 3000) -> str:
    """The sentences that talk most about this field, in document order, for the candidate pick."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    scored = sorted(range(len(sentences)), key=lambda i: -field_score(sentences[i], field))
    keep, size = [], 0
    for i in scored:
        if field_score(sentences[i], field) == 0 or size + len(sentences[i]) > limit:
            break
        keep.append(i)
        size += len(sentences[i]) + 1
    return " ".join(sentences[i] for i in sorted(keep))


def candidates(text: str, field: str) -> list[dict]:
    """Numbers of the field's kind written in `text`: [{'value', 'written'}], distinct values, in order."""
    out, seen = [], set()
    for match in CANDIDATE[FIELD_CANDIDATES[field]].finditer(text):
        kind = FIELD_CANDIDATES[field]
        if kind == "price":
            dollars, cents = match.group(1), match.group(2)
            value = float(dollars) if dollars else float(cents) / 100
        else:
            value = float(match.group(1).replace(",", ""))
            scale = (match.group(2) or "").lower() if kind in ("money", "shares") and match.lastindex >= 2 else ""
            value *= 1e6 if scale in ("million", "m") else 1e3 if scale in ("thousand", "k") else 1
            if kind == "area" and match.group(2).lower().startswith(("km", "sq", "square")):
                value *= 100                                      # every candidate area is in hectares
        if value and value not in seen:
            seen.add(value)
            out.append({"value": value, "written": match.group(0).strip()})
    return out[:MAX_CANDIDATES]


def hectares(value, unit) -> float | None:
    return end_to_end.hectares(value, unit)


# --- Jev: judge ---------------------------------------------------------------------------------------------------

def pick(project: str, field: str, text_passage: str, options: list[dict], current: float | None) -> dict:
    """Jev picks which candidate is this field of this deal, or none. Returns the top pick and its probability."""
    definition = FIELDS[field][0]
    criteria = {f"o{i}": f"{o['written']} (that is {o['value']:,.10g})" for i, o in enumerate(options, 1)}
    criteria["none"] = f"None of these numbers is {definition} for this project."
    answer = jev.decide({"project": project, "field": definition, "document_passage": text_passage[:3000]},
                        {"pick": jev.choice(f"Which number in document_passage is {definition} for the project named in "
                                            "state? Pick none if no number is.", criteria)})["pick"]
    key = answer["choice"]
    chosen = None if key == "none" else options[int(key[1:]) - 1]["value"]
    probability = (answer.get("probabilities") or {}).get(key, answer.get("confidence"))
    return {"proposal": chosen, "p_proposal": probability, "proposal_is_current": chosen is not None and current is not None
            and abs(chosen - current) <= max(0.011, 0.01 * abs(current))}


def action(p: float | None, picked: dict | None, field: str | None = None) -> str:
    """accept, correct (only for a field that holds one written number), or review. A proposal for a field that can
    be a sum stays a proposal: it is shown in the review queue, not written."""
    if p is not None and p >= ACCEPT:
        return "accept"
    if picked and picked["proposal"] is not None and not picked["proposal_is_current"] and (picked["p_proposal"] or 0) >= ACCEPT \
            and (p is None or p < REVIEW) and (field is None or field in SINGLE_NUMBER_FIELDS):
        return "correct"
    return "review"


SPLIT_FIELDS = ("cash_payments_value", "exp_comit_value", "shares_value", "shares_amount", "aggregate_deal_value",
                "project_area", "drilling_amount")
PARTS = ("cash_payments_value", "exp_comit_value", "shares_value")
FORMULAS = {"cash + exploration commitment + shares value": PARTS, "shares_amount x share_price": ("shares_amount", "share_price")}
ORDER = {"aggregate_deal_value": 2, "shares_value": 1}     # a formula's parts are checked before it


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(1.0, 0.01 * abs(b))


SHARED = jev.noul(
    "Does document_passage say that the total in state is shared between several projects or properties, so that each "
    "project's part is the total divided equally?",
    yes="The total covers several named projects, properties or tenements together, for example 'a commitment of "
        "$1,000,000 across the Bingara and Nundle projects', and the project in state is one of them.",
    no="The total belongs to one project only; or it is not a payment, commitment, share count or area of this deal; "
       "or the passage does not connect it to several projects.")


OPENING_CHARS = 1200
# Fields that hold one written number. Cash, commitments, share counts and values can be sums of several written
# numbers, and Jev's pick can only choose one, so for those a pick is shown for review, never applied.
SINGLE_NUMBER_FIELDS = ("interest_acquired_percent", "nsr_percent", "share_price", "project_area", "drilling_amount")


def projects_named(project: str) -> int:
    """How many projects a group name lists: 'Telfer and Havieron' -> 2, 'A, B & C' -> 3, 'Foggy Mountain' -> 1."""
    return len([p for p in re.split(r"\s*(?:,|&|\band\b)\s*", project or "") if p.strip()]) or 1


def money_spans(text: str, x: float) -> list[tuple[int, int]]:
    """Places where `x` is written as money (a currency sign, or 'dollars' / 'in cash' after it)."""
    return [s for s in number_spans(text, x, kind=MONEY) if occurrence_kind(text, *s) == MONEY
            or _CURRENCY_BEFORE.search(text[max(0, s[0] - 5):s[0]])]


def rule_source(text: str, field: str, value: float, extraction: dict, check: dict | None = None
                ) -> tuple[str, list[float]] | None:
    """A client rule that makes `value` follow from what is written, done in code: (description, the inputs).
    Rules from the client's field definitions: aggregate = cash + exploration + shares value, shares value =
    shares x price, the sum of all payments of one kind, drilling at USD 250 per metre, and an equal split across
    projects. A split found here is only a candidate: check_cell asks Jev whether the total is shared."""
    kind = FIELD_KIND.get(field)
    if field == "aggregate_deal_value":
        parts = [to_number(extraction.get(n)) for n in PARTS]
        if any(parts) and _close(sum(x for x in parts if x), value):
            return "cash + exploration commitment + shares value", [x for x in parts if x]
    if field == "shares_value":
        amount, price = to_number(extraction.get("shares_amount")), to_number(extraction.get("share_price"))
        if amount and price and _close(amount * price, value):
            return "shares_amount x share_price", [amount, price]
    if check and check.get("status") == DERIVED and check.get("evidence"):
        evidence = check["evidence"]          # the validator's sum, split or metres rule, with its written inputs
        return evidence, [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", evidence) if float(n.replace(",", "")) > 6]
    if field == "exp_comit_value":
        metres = value / DRILLING_USD_PER_METRE
        if metres == int(metres) and re.search(r"(?<![\d.,])" + re.escape(f"{int(metres):,}").replace(",", ",?")
                                               + r"\s*(?:m\b|metres|meters)", text):
            return f"{int(metres):,} metres x {DRILLING_USD_PER_METRE} USD", [metres]
    if field in SPLIT_FIELDS:
        for n in range(2, 7):
            total = round(value * n)
            if abs(value * n - total) > 0.01 * n:
                continue
            found = money_spans(text, total) if kind == MONEY else number_spans(text, total, kind=kind)
            if found:                                     # a money total must be written as money, not "5 million tonnes"
                return f"{total:,} split across {n} projects", [total]
    return None


def check_cell(text: str, project: str, field: str, value: float, unit: str | None, check: dict | None,
               ticker: str | None, news_date: str | None, extraction: dict | None = None,
               done: dict | None = None, project_count: int = 1) -> dict:
    """`done` holds the cells of this row already checked, so a computed value can take its parts' verdicts."""
    kind, percent = FIELD_KIND.get(field), field in PERCENT_FIELDS
    spans = number_spans(text, value, percent=percent, kind=kind)
    cell = {"field": field, "value": value, "unit": unit, "places_written": len(spans), "in_document": bool(spans),
            "validator": (check or {}).get("status")}
    p, best, inputs = None, None, []
    rule = None if spans or percent else rule_source(text, field, value, extraction or {}, check)
    if rule and "split across" in rule[0]:
        total, n = rule[1][0], int(re.search(r"split across (\d+)", rule[0]).group(1))
        if project_count == n or projects_named(project) == n:
            # code knows the row is one of n projects (the pipeline's one-row-per-project count): no question to ask
            cell["split_by"] = "project count"            # "Telfer and Havieron": two projects, written in code-readable form
        else:
            # Otherwise whether the total is shared is a judgment about the text: Jev decides, code divides.
            places = best_places(text, field, number_spans(text, total, kind=kind)) or best_places(text, field, number_spans(text, total))
            opening = text[:OPENING_CHARS]
            shared = max((jev.decide({"project": project, "total": f"{total:,.10g}", "number_of_projects": n,
                                      "announcement_opening": opening, "document_passage": place},
                                     {"shared": SHARED})["shared"]["noul"] for place in places), default=0.0)
            cell["p_shared_between_projects"] = shared
            if shared < REVIEW:
                rule = None                              # the division matched a written number by chance
    if spans:
        cell["source"] = "stated"
        for place in best_places(text, field, spans):
            verdict = ask(project, field, value, unit, "written in the document", place)
            if p is None or verdict["p_correct"] > p:
                p, best = verdict["p_correct"], {"passage": place, "role": verdict["role"]}
    elif rule:
        description, inputs = rule
        cell["source"] = f"computed: {description}"
        formula_parts = FORMULAS.get(description)
        if formula_parts:
            verdicts = [(done or {}).get(n, {}).get("p_correct") for n in formula_parts if to_number((extraction or {}).get(n))]
            if verdicts and all(v is not None for v in verdicts):
                p = min(verdicts)                   # a formula is as sound as its weakest part
        else:
            # Jev judges the written inputs' passages: are these amounts this field of this deal? Code did the sum.
            place = passage(text, field, value, " ".join(f"{x:,.10g}" for x in inputs), DERIVED)
            if place:
                verdict = ask(project, field, value, unit, f"computed by code from the written numbers: {description}; "
                              "judge only whether those written numbers are this field of this deal", place)
                p, best = verdict["p_correct"], {"passage": place, "role": verdict["role"]}
    elif field == "share_price":
        close = market.close_on(ticker, news_date)
        cell["source"], cell["market"] = "market", close
        if close:
            matches = abs(close["close"] - value) <= PRICE_TOLERANCE * close["close"]
            cell["action"] = "accept" if matches else "correct"
            cell["proposal"], cell["p_proposal"] = (None, None) if matches else (close["close"], 1.0)
            return cell
    else:
        cell["source"] = "not_in_document"
    cell["p_correct"] = p
    cell.update(best or {})
    picked = None
    is_formula = cell["source"].removeprefix("computed: ") in FORMULAS
    if (p is None or p < ACCEPT) and not is_formula:      # a formula is corrected through its parts, never by a pick
        text_for_pick = field_passage(text, field)
        options = candidates(text_for_pick, field)
        if options:
            picked = pick(project, field, text_for_pick, options, value if field != "project_area" else hectares(value, unit))
            if picked["proposal"] is not None and any(_close(picked["proposal"], x) for x in inputs):
                picked["proposal_is_input"] = True   # Jev picked the number the rule starts from: no correction
                picked["proposal_is_current"] = True
            cell.update(picked)
    cell["action"] = action(p, picked, field)
    return cell


def companies(text_passage: str, also: list[str | None]) -> list[str]:
    names = []
    for name in [*also, *(m.group(1).strip() for m in _COMPANY.finditer(text_passage))]:
        if name and company_words(name) and not any(set(company_words(name)) & set(company_words(n)) for n in names):
            names.append(name)
    return names[:MAX_COMPANIES]


def check_parties(text: str, extraction: dict, project: str, analyst_owner: str | None) -> dict:
    owner, ticker, deal_type = extraction.get("owner_name"), extraction.get("owner_ticker"), extraction.get("deal_type")
    text_passage = party_passage(text, owner or analyst_owner or project or "")
    names = companies(text_passage, [owner, analyst_owner])
    out = {"owner_name": owner, "owner_ticker": ticker, "deal_type": deal_type, "analyst_owner": analyst_owner,
           "companies": names, "passage": text_passage}
    if names:
        criteria = {f"c{i}": f"the company {n}" for i, n in enumerate(names, 1)}
        criteria["none"] = "None of these companies acquires the interest in the project."
        answer = jev.decide({"project": project, "document_passage": text_passage},
                            {"buyer": jev.choice("Which company is the buyer in this transaction: the one that acquires, "
                                                 "or can earn, the interest in the project and pays the consideration?",
                                                 criteria)})["buyer"]
        probabilities = answer.get("probabilities") or {}
        out["p_buyer_by_company"] = {names[int(k[1:]) - 1] if k != "none" else "none": v for k, v in probabilities.items()}
        out["buyer_pick"] = answer["choice"] if answer["choice"] == "none" else names[int(answer["choice"][1:]) - 1]
    if owner:
        state = {"named_company": owner, "project": project, "document_passage": text_passage}
        questions = {"named_company_is_buyer": BUYER, "role": ROLE, "project_is_object": PROJECT}
        if ticker:
            state["ticker"], questions["ticker_is_buyers"] = ticker, TICKER
        if deal_type:
            state["deal_type"], questions["deal_type_fits"] = deal_type, DEAL_TYPE
        a = jev.decide(state, questions)
        out.update(p_owner_is_buyer=a["named_company_is_buyer"]["noul"], owner_role=a["role"]["choice"],
                   p_project_is_object=a["project_is_object"]["noul"],
                   p_ticker_is_buyers=a.get("ticker_is_buyers", {}).get("noul"),
                   p_deal_type_fits=a.get("deal_type_fits", {}).get("noul"))
        p_owner = (out.get("p_buyer_by_company") or {}).get(owner, out["p_owner_is_buyer"])
        top = out.get("buyer_pick")
        p_top = (out.get("p_buyer_by_company") or {}).get(top, 0)
        out["action"] = "accept" if out["p_owner_is_buyer"] >= ACCEPT else "review"
        if out["p_owner_is_buyer"] < REVIEW and top not in (None, "none", owner) and p_top >= ACCEPT:
            # A correction needs three answers to agree: the stored owner is not the buyer, the proposed company is
            # the top pick, and asked on its own, the proposed company is the buyer.
            second = jev.decide({**state, "named_company": top}, {"named_company_is_buyer": BUYER})
            out["p_proposal_is_buyer"] = second["named_company_is_buyer"]["noul"]
            if out["p_proposal_is_buyer"] >= ACCEPT:
                out["action"], out["proposal"] = "correct", top
        out["p_owner_in_pick"] = p_owner
    return out


# --- run --------------------------------------------------------------------------------------------------------

def check_run(run_dir: Path, rows_path: Path = end_to_end.DEFAULT_ROWS) -> tuple[list[dict], list[dict]]:
    rows = end_to_end.load_rows(rows_path)
    cells, parties = [], []
    for entry in json.loads((run_dir / "run_report.json").read_text()):
        if not entry.get("validation"):
            continue
        raw, _ = pdf_text(Path(entry["source_url"].removeprefix("file://")).read_bytes())
        text = " ".join(raw.split())
        extraction = {**(entry.get("extraction") or {}), **(entry.get("withheld") or {})}   # withheld values are checked too
        project = entry.get("project_name") or ""
        row = rows.get(entry["id"], {})
        base = {"row_id": entry["id"], "project": project, "project_count": entry.get("project_count") or 1}
        done: dict[str, dict] = {}
        for field, check in sorted(entry["validation"]["fields"].items(), key=lambda kv: ORDER.get(kv[0], 0)):
            value = to_number(extraction.get(field))
            if field not in FIELDS or not value:
                continue
            unit = extraction.get("project_area_unit") if field == "project_area" else extraction.get(end_to_end.CURRENCIES.get(field, ""))
            # the stored date (the release's), not the analyst's, some of which have day and month swapped
            cell = check_cell(text, project, field, value, unit, check, extraction.get("owner_ticker"),
                              extraction.get("news_date") or (entry.get("date") or "")[:10] or None, extraction, done,
                              project_count=base["project_count"])
            cell["withheld"] = field in (entry.get("withheld") or {})
            done[field] = cell
            cells.append({**base, **cell})
        parties.append({**base, **check_parties(text, extraction, project, row.get("owner_name"))})
    return cells, parties


def calibration(cells: list[dict], parties: list[dict], comparison: list[dict]) -> list[str]:
    """How the actions line up with the analyst's table, leaving out the rows known to be wrong there."""
    verdict = {(c["row_id"], c["field"]): c["verdict"] for c in comparison}
    table = Counter()
    for c in cells:
        if (c["row_id"], c["field"]) in KNOWN_DATA_PROBLEMS:
            continue
        table[(verdict.get((c["row_id"], c["field"]), "no analyst value"), c["action"])] += 1
    owner = Counter()
    for p in parties:
        if "action" not in p or (p["row_id"], "owner_name") in KNOWN_DATA_PROBLEMS:
            continue
        same = bool(p["analyst_owner"]) and bool(set(company_words(p["owner_name"])) & set(company_words(p["analyst_owner"])))
        owner[("same owner as analyst" if same else "different owner", p["action"])] += 1
    return (["| Analyst comparison | accept | correct | review |", "|---|---|---|---|"]
            + [f"| {name} | {table[(name, 'accept')]} | {table[(name, 'correct')]} | {table[(name, 'review')]} |"
               for name in ("agree", "disagree", "pipeline_only", "no analyst value")]
            + ["", "| Owner against analyst | accept | correct | review |", "|---|---|---|---|"]
            + [f"| {name} | {owner[(name, 'accept')]} | {owner[(name, 'correct')]} | {owner[(name, 'review')]} |"
               for name in ("same owner as analyst", "different owner")])


def summary(cells: list[dict], parties: list[dict], comparison: list[dict]) -> str:
    sources = Counter(c["source"].split(":")[0] for c in cells)
    actions = Counter(c["action"] for c in cells)
    out = ["", f"## Cell check (`{jev.MODEL}`, accept at P >= {ACCEPT}, review below; Jev for meaning, code for numbers)\n",
           f"- Cells checked: {len(cells)}; source: {dict(sources)}; action: {dict(actions)}",
           f"- Parties checked: {len(parties)}; action: {dict(Counter(p.get('action', 'no owner') for p in parties))}",
           f"- Jev calls: {jev.spend['calls']} paid, {jev.spend['cached']} cached, {jev.spend['usd']:.5f} USD", "",
           "### Calibration against the analyst's table (known analyst errors left out)\n", *calibration(cells, parties, comparison),
           "", "### Cells not accepted\n", "| Row | Field | Value | Source | P(correct) | Proposal (P) | Action |", "|---|---|---|---|---|---|---|"]
    for c in cells:
        if c["action"] != "accept":
            proposal = "-" if c.get("proposal") is None else f"{c['proposal']:,.10g} ({c.get('p_proposal') or 0:.2f})"
            p = "-" if c.get("p_correct") is None else f"{c['p_correct']:.2f}"
            out.append(f"| {c['row_id']} | `{c['field']}` | {c['value']:,.10g} | {c['source'][:40]} | {p} | {proposal} | {c['action']} |")
    out += ["", "### Buyer probabilities where the owner is not accepted\n",
            "| Row | Stored owner | P(owner is buyer) | Buyer by company | Analyst owner | Action |", "|---|---|---|---|---|---|"]
    for p in parties:
        if p.get("action") not in (None, "accept"):
            by_company = ", ".join(f"{k} {v:.2f}" for k, v in sorted((p.get("p_buyer_by_company") or {}).items(), key=lambda kv: -kv[1])[:3])
            out.append(f"| {p['row_id']} | {p['owner_name']} | {p['p_owner_is_buyer']:.2f} | {by_company} | {p['analyst_owner']} | "
                       f"{p['action']}{' to ' + p['proposal'] if p.get('proposal') else ''} |")
    return "\n".join(out) + "\n"


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--rows", type=Path, default=end_to_end.DEFAULT_ROWS)
    args = parser.parse_args(argv)
    telemetry.setup()
    try:
        cells, parties = check_run(args.run_dir, args.rows)
    finally:
        telemetry.shutdown()
    for name, lines in (("cells.jsonl", cells), ("parties.jsonl", parties)):
        (args.run_dir / name).write_text("".join(json.dumps(x, ensure_ascii=False, default=str) + "\n" for x in lines))
    comparison = [json.loads(line) for line in (args.run_dir / "comparison.jsonl").read_text().splitlines()]
    text = summary(cells, parties, comparison)
    summary_path = args.run_dir / "summary.md"
    before = summary_path.read_text().split("\n## Cell check")[0].rstrip("\n") + "\n"
    summary_path.write_text(before + text)
    print(text)


if __name__ == "__main__":
    main()
