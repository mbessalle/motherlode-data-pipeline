"""Deterministic checks on an extraction, with no model involved.

For every number the model returned, decide whether the source text supports it:

  FOUND      the value is written in the article (the snippet is kept as evidence)
  DERIVED    not written, but explained by written amounts through one of the extraction rules:
             a sum of payments, a total split across N projects, shares x price, or the sum of the parts
  NOT_FOUND  neither: the model may have invented it, read another deal, or the source is incomplete
  MARKET     a share price or market cap not stated in the announcement: market data for the client, checked
             against the market elsewhere, never blocking
  EMPTY      nothing was extracted (recall is measured by the eval set, not here)

`validate()` turns that into a decision the pipeline can route on. Only NOT_FOUND values block a write.
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field

from . import schema

FOUND, DERIVED, NOT_FOUND, EMPTY, MARKET = "FOUND", "DERIVED", "NOT_FOUND", "EMPTY", "MARKET"

MONEY_FIELDS = ("cash_payments_value", "exp_comit_value")
PERCENT_FIELDS = ("nsr_percent", "interest_acquired_percent")
PLAIN_FIELDS = ("shares_amount", "share_price", "project_area", "drilling_amount")
MARKET_FIELDS = ("share_price", "owner_marketcap")      # market data for the client, not required in the announcement
CHECKED_FIELDS = MONEY_FIELDS + ("shares_amount", "share_price", "shares_value", "aggregate_deal_value") + PERCENT_FIELDS + (
    "project_area", "drilling_amount", "shares_value_scraped", "drilling_value", "owner_marketcap")
_ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve", "thirteen",
         "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def number_in_words(n: int) -> str | None:
    """1-100 as a regex fragment: 75 -> seventy[- ]five, 100 -> (?:one[- ])?hundred. Press releases often spell percentages."""
    if 1 <= n < 20:
        return _ONES[n]
    if 20 <= n < 100:
        return _TENS[n // 10] + (r"[- ]" + _ONES[n % 10] if n % 10 else "")
    return r"(?:one[- ])?hundred" if n == 100 else None


DRILLING_USD_PER_METRE = 250  # extraction rule: commitments given in metres are converted at 250 USD per metre


def to_number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(str(value).replace(",", "").replace("$", ""))
    except ValueError:
        return None


def _forms(x: float) -> set[str]:
    """Ways a number is written: 2000000, 2,000,000, 0.25, .25"""
    s = f"{x:.6f}".rstrip("0").rstrip(".")
    if "." in s:
        whole, frac = s.split(".")
        return {s, f"{int(whole):,}.{frac}"} | ({f".{frac}"} if whole == "0" else set())
    return {s, f"{int(s):,}"}


MONEY, SHARES, OPTIONS, OTHER = "money", "shares", "options", "other"
FIELD_KIND = {"cash_payments_value": MONEY, "exp_comit_value": MONEY, "shares_value": MONEY, "aggregate_deal_value": MONEY,
              "shares_value_scraped": MONEY, "drilling_value": MONEY, "owner_marketcap": MONEY, "shares_amount": SHARES}
_CURRENCY_BEFORE = re.compile(r"(?:[A-Z]{0,2}\$|\b(?:CAD|USD|AUD|EUR|GBP|NZD))\s?$")
_CURRENCY_LOOKBEHIND = r"(?:(?<=\$)|(?<=\$ )|(?<=CAD )|(?<=USD )|(?<=AUD )|(?<=CAD)|(?<=USD)|(?<=AUD))"
_MONEY_AFTER = re.compile(r"\s*(?:million\s+|thousand\s+)?(?:dollars|in cash)\b", re.I)
_SHARES_AFTER = re.compile(r"\s*(?:million\s+|thousand\s+)?(?:[a-z-]+\s){0,3}(?:shares|units)\b", re.I)
# options, warrants and performance rights are not consideration shares and never enter a share count (critic N6)
_OPTIONS_AFTER = re.compile(r"\s*(?:million\s+|thousand\s+)?(?:[a-z-]+\s){0,3}"
                            r"(?:options|warrants|performance rights|rights)\b", re.I)
_CURRENCY_CODES = ("AUD", "CAD", "USD", "EUR", "GBP", "NZD", "US", "A", "C")


def glued_to_letters(text: str, start: int) -> bool:
    """A number glued to letters is part of an identifier ("BVRB105", a drill hole), not a value; a currency code
    glued to it ("AUD250,000", "US$") is still a value (critic N4)."""
    if start == 0 or not text[start - 1].isalpha():
        return False
    letters = re.search(r"[A-Za-z]+$", text[max(0, start - 6):start]).group(0)
    return letters.upper() not in _CURRENCY_CODES


def occurrence_kind(text: str, start: int, end: int) -> str:
    """What a number at text[start:end] counts: money, a share count, or neither."""
    if _CURRENCY_BEFORE.search(text[max(0, start - 5):start]) or _MONEY_AFTER.match(text[end:end + 30]):
        return MONEY
    if _OPTIONS_AFTER.match(text[end:end + 50]):
        return OPTIONS
    if _SHARES_AFTER.match(text[end:end + 50]):
        return SHARES
    return OTHER


def find_number(text: str, x: float, percent: bool = False, kind: str | None = None) -> str | None:
    """Return the surrounding snippet if `x` is written in `text`, else None.

    With `kind`, an occurrence of the other kind does not count: 500,000 written as "500,000 shares"
    is not evidence for a 500,000 cash payment, and "$500,000" is not evidence for a share count."""
    patterns = []
    for form in _forms(x):
        tail = r"\s*(?:%|per\s?cent|percent)" if percent else r"(?![\d]|[.,]\d)"
        patterns.append(r"(?<![\d.,])" + re.escape(form) + r"(?:\.0+)?" + tail)
    if percent and x == int(x) and number_in_words(int(x)):
        # the lookbehind stops "five percent" matching inside "seventy-five percent"
        patterns.append(r"(?<!ty[- ])\b" + number_in_words(int(x)) + r"\s*(?:per\s?cent|percent)")
    if not percent and x >= 1e5:  # "2 million", "$1.5M", "250K"
        patterns += [r"(?<![\d.,])" + re.escape(f) + r"\s*(?:million|mm\b)" for f in _forms(x / 1e6)]
        # a bare "1m" is a metre in an exploration release ("1m @ 6.15 g/t"); it is a million only after a currency sign
        patterns += [_CURRENCY_LOOKBEHIND + re.escape(f) + r"\s*m\b" for f in _forms(x / 1e6)]
        patterns += [r"(?<![\d.,])" + re.escape(f) + r"\s*(?:thousand|k\b)" for f in _forms(x / 1e3)]
    other = {MONEY: (SHARES, OPTIONS), SHARES: (MONEY, OPTIONS)}.get(kind, ())
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.I):
            if glued_to_letters(text, match.start()):
                continue
            if other and occurrence_kind(text, match.start(), match.end()) in other:
                continue
            start, end = max(0, match.start() - 90), min(len(text), match.end() + 90)
            return re.sub(r"\s+", " ", text[start:end]).strip()
    return None


_AMOUNT = re.compile(
    r"(?P<cur>(?:[A-Z]{0,2}\$|\b(?:CAD|USD|AUD|EUR|GBP|NZD)\b)\s?)?(?<![\d.,])(?P<num>\d{1,3}(?:,\d{3})+|\d+)(?P<frac>\.\d+)?"
    r"\s*(?P<unit>million|thousand|m\b|k\b)?(?=(?P<after>[^.;\n|]{0,40}))", re.I)


MARKET_TOLERANCE = 0.10   # a share price within 10% of the market close counts as the market price
SUM_WINDOW = 1500   # the terms of a sum must be written within this many characters of each other
_COUNT_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "both": 2}
# Only project nouns count, with real words between ("the five Option Projects"); tenements and licences do not,
# because one project often has several ("70% of 3 tenements"), and "Figure 2 - Project Location" is a caption.
_STATED_COUNT = re.compile(r"(?<!figure )(?<!fig\. )(?<!table )(?<!appendix )(?<!section )"
                           r"\b(2|3|4|5|6|two|three|four|five|six|both)\s+(?:[a-z]+\s){0,2}"
                           r"(?:projects|properties|project areas)\b", re.I)


def written_amounts_at(text: str, exclude: str | None = None, only: str | None = None) -> list[tuple[float, int]]:
    """Amounts written in the text with their positions, with repeats. A bare number such as a year does not
    count: it needs a thousands separator, a currency sign, or a million/thousand unit. "m" counts as million only
    after a currency sign ("$1m"), because "1m" in an exploration release is a metre.

    Each amount has a kind (money, a share count, or other) so that a sum never mixes dollars with shares;
    `exclude` drops one kind."""
    out = []
    for m in _AMOUNT.finditer(text):
        unit = (m.group("unit") or "").lower()
        if unit == "m" and not m.group("cur"):
            unit = ""
        if not (m.group("cur") or "," in m.group("num") or unit):
            continue
        value = float(m.group("num").replace(",", "") + (m.group("frac") or ""))
        value *= 1e6 if unit in ("million", "m") else 1e3 if unit in ("thousand", "k") else 1
        if value < 1000:
            continue
        if glued_to_letters(text, m.start("num")) and not m.group("cur"):
            continue
        kind = MONEY if m.group("cur") else occurrence_kind(text, m.start("num"), m.end())
        if kind != exclude and kind != OPTIONS and (only is None or kind == only):
            out.append((value, m.start("num")))
    return out


def written_amounts(text: str, exclude: str | None = None) -> list[float]:
    """The values of written_amounts_at, sorted."""
    return sorted(value for value, _ in written_amounts_at(text, exclude))


def stated_project_counts(text: str) -> set[int]:
    """Numbers of projects the text states: 'the five Option Projects', '2 project areas', 'both tenements'."""
    return {int(n) if n.isdigit() else _COUNT_WORDS[n.lower()] for n in _STATED_COUNT.findall(text)}


def explain_by_rules(x: float, amounts: list, text: str, kind: str | None = None, project_count: int = 1) -> str | None:
    """A sum of up to three written amounts written near each other, or a written total (or two-term sum) split
    across N projects, where N is the row's number of projects or a number of projects the text states.
    An amount is used twice only if it is written twice. This is a plausibility test, not a proof: without the
    count and distance limits it accepted 4% to 36% of unwritten round numbers in the seed-7 sample."""
    pairs = [(a, None) if isinstance(a, int | float) else a for a in amounts]

    def as_sum(target: float, max_terms: int) -> str | None:
        candidates = [p for p in pairs if p[0] < target]
        for n in range(2, max_terms + 1):
            for combo in itertools.combinations(candidates, n):
                positions = [pos for _, pos in combo if pos is not None]
                if positions and max(positions) - min(positions) > SUM_WINDOW:
                    continue
                if abs(sum(v for v, _ in combo) - target) <= 0.5:
                    return " + ".join(f"{v:,.0f}" for v, _ in combo)
        return None

    summed = as_sum(x, 3)
    if summed:
        return "sum of " + summed
    counts = stated_project_counts(text) | ({project_count} if project_count and project_count > 1 else set())
    for n in sorted(c for c in counts if 2 <= c <= 6):
        total = x * n
        if total < 10000 or abs(total - round(total)) > 0.01 * n:
            continue
        total = round(total)
        if find_number(text, total, kind=kind):
            return f"{total:,.0f} split across {n} projects"
        summed = as_sum(total, 2)
        if summed:
            return f"({summed}) split across {n} projects"
    return None


def _close(a: float, b: float, tolerance: float = 0.01) -> bool:
    return abs(a - b) <= max(1.0, tolerance * abs(b))


@dataclass
class FieldCheck:
    status: str
    value: float | None = None
    evidence: str | None = None


@dataclass
class Report:
    fields: dict[str, FieldCheck] = field(default_factory=dict)
    schema_issues: list[str] = field(default_factory=list)

    @property
    def not_found(self) -> list[str]:
        return [name for name, check in self.fields.items() if check.status == NOT_FOUND]

    @property
    def checked(self) -> int:
        return sum(1 for c in self.fields.values() if c.status in (FOUND, DERIVED, NOT_FOUND))

    @property
    def grounded(self) -> int:
        return sum(1 for c in self.fields.values() if c.status in (FOUND, DERIVED))

    @property
    def passed(self) -> bool:
        return not self.not_found and not self.schema_issues

    def feedback(self) -> str:
        """What to tell the model on a retry."""
        lines = [f"- {name}: {self.fields[name].value:g}" for name in self.not_found]
        lines += [f"- {issue}" for issue in self.schema_issues]
        return "\n".join(lines)

    def as_dict(self) -> dict:
        return {
            "passed": self.passed, "checked": self.checked, "grounded": self.grounded, "not_found": self.not_found,
            "schema_issues": self.schema_issues,
            "fields": {k: {"status": c.status, "value": c.value, "evidence": c.evidence} for k, c in self.fields.items()},
        }


def validate(extraction: dict, article_text: str, project_count: int = 1, market_price: dict | None = None) -> Report:
    """`project_count` is the number of projects the row stands for (a deal over "A and B" processed as one row per
    project gives 2), so a total shared by that many projects is accepted when split. `market_price` is the buyer's
    close on the news date ({'close', 'date', 'symbol'}), looked up by code; a share price not written in the
    announcement counts for derived values only when it matches that close."""
    text = article_text.replace("\xa0", " ").replace("’", "'")
    # a share count is summed only from amounts written as shares, never from options or table numbers (critic N6)
    money_amounts, share_amounts = written_amounts_at(text, exclude=SHARES), written_amounts_at(text, only=SHARES)
    report = Report(schema_issues=schema.issues(extraction))
    values = {name: to_number(extraction.get(name)) for name in CHECKED_FIELDS}

    for name in CHECKED_FIELDS:
        x = values[name]
        if x is None or x == 0:
            report.fields[name] = FieldCheck(EMPTY)
            continue
        snippet = find_number(text, x, percent=name in PERCENT_FIELDS, kind=FIELD_KIND.get(name))
        if snippet:
            report.fields[name] = FieldCheck(FOUND, x, snippet)
        elif name in MARKET_FIELDS:
            # The client defines share price (and market cap) as market data on the news date, not a number the
            # announcement must state, so it never blocks. A price counts as support for shares_value only when it
            # matches the close code looked up (critic N7); an unconfirmed one is only stored.
            close = (market_price or {}).get("close") if name == "share_price" else None
            if close and _close(x, close, MARKET_TOLERANCE):
                where = f"{market_price['date']} ({market_price['symbol']})"
                report.fields[name] = FieldCheck(MARKET, x, f"close {close:g} on {where}")
            else:
                report.fields[name] = FieldCheck(MARKET, x, "market data, not confirmed")
        else:
            report.fields[name] = FieldCheck(NOT_FOUND, x)

    # An area is only an area in a unit that can be converted to hectares ("33 sub-blocks" is not one).
    unit = (extraction.get("project_area_unit") or "").strip().lower()
    if values["project_area"] and schema.HECTARES_PER_UNIT.get(unit) is None:
        why = f"unit {unit!r} is not an area" if unit else "no unit, so it cannot be stored in hectares"
        report.fields["project_area"] = FieldCheck(NOT_FOUND, values["project_area"], why)

    def supported(name: str) -> bool:
        check = report.fields[name]
        return check.status in (FOUND, DERIVED) or (check.status == MARKET and (check.evidence or "").startswith("close"))

    def derive(name: str, evidence: str | None) -> None:
        if evidence and report.fields[name].status == NOT_FOUND:
            report.fields[name] = FieldCheck(DERIVED, values[name], evidence)

    # Payments, share counts and share values are often a sum of tranches, or one total shared by several projects.
    for name in MONEY_FIELDS + ("shares_value", "shares_value_scraped", "shares_amount"):
        if report.fields[name].status == NOT_FOUND:
            amounts = share_amounts if name == "shares_amount" else money_amounts
            derive(name, explain_by_rules(values[name], amounts, text, FIELD_KIND.get(name), project_count))

    # The drilling value is the committed metres at the client's rate.
    if values["drilling_value"] and values["drilling_amount"] and supported("drilling_amount") \
            and _close(values["drilling_amount"] * DRILLING_USD_PER_METRE, values["drilling_value"]):
        derive("drilling_value", f"drilling_amount x {DRILLING_USD_PER_METRE} USD")

    # Exploration commitments stated in metres are converted at a fixed rate.
    if report.fields["exp_comit_value"].status == NOT_FOUND:
        metres = values["exp_comit_value"] / DRILLING_USD_PER_METRE
        if metres == int(metres) and re.search(r"(?<![\d.,])" + re.escape(f"{int(metres):,}").replace(",", ",?")
                                               + r"\s*(?:m\b|metres|meters)", text):   # metres, not "$4,000" (N8)
            derive("exp_comit_value", f"{metres:,.0f} metres x {DRILLING_USD_PER_METRE} USD")

    # shares_value is stated in the article, or it is shares x price with both inputs supported.
    if values["shares_value"] and values["shares_amount"] and values["share_price"] \
            and supported("shares_amount") and supported("share_price") \
            and _close(values["shares_amount"] * values["share_price"], values["shares_value"]):
        derive("shares_value", "shares_amount x share_price")

    # ...or shares x a per-share price the article states, even when the model did not return that price.
    if values["shares_value"] and values["shares_amount"] and supported("shares_amount") \
            and report.fields["shares_value"].status == NOT_FOUND:
        implied = values["shares_value"] / values["shares_amount"]
        for decimals in (2, 3, 4):
            price = round(implied, decimals)
            if price > 0 and _close(values["shares_amount"] * price, values["shares_value"], 0.001) and find_number(text, price):
                derive("shares_value", f"shares_amount x {price:g} (price written in the article)")
                break

    # The aggregate is the sum of its parts, accepted only when every part is itself supported.
    if values["aggregate_deal_value"]:
        parts = [n for n in ("cash_payments_value", "exp_comit_value", "shares_value") if values[n]]
        if parts and all(supported(n) for n in parts) and _close(sum(values[n] for n in parts), values["aggregate_deal_value"]):
            derive("aggregate_deal_value", " + ".join(parts))

    return report
