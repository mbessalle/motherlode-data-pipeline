"""Deterministic checks on an extraction, with no model involved.

For every number the model returned, decide whether the source text supports it:

  FOUND      the value is written in the article (the snippet is kept as evidence)
  DERIVED    not written, but explained by written amounts through one of the extraction rules:
             a sum of payments, a total split across N projects, shares x price, or the sum of the parts
  NOT_FOUND  neither: the model may have invented it, read another deal, or the source is incomplete
  EMPTY      nothing was extracted (recall is measured by the eval set, not here)

`validate()` turns that into a decision the pipeline can route on. Only NOT_FOUND values block a write.
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from datetime import date

FOUND, DERIVED, NOT_FOUND, EMPTY = "FOUND", "DERIVED", "NOT_FOUND", "EMPTY"

MONEY_FIELDS = ("cash_payments_value", "exp_comit_value")
PERCENT_FIELDS = ("nsr_percent", "interest_acquired_percent")
PLAIN_FIELDS = ("shares_amount", "share_price", "project_area", "drilling_amount")
CHECKED_FIELDS = MONEY_FIELDS + ("shares_amount", "share_price", "shares_value", "aggregate_deal_value") + PERCENT_FIELDS + (
    "project_area", "drilling_amount")
CURRENCY_FIELDS = ("aggregate_deal_currency", "cash_payments_currency", "exp_comit_currency", "shares_currency")
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


MONEY, SHARES, OTHER = "money", "shares", "other"
FIELD_KIND = {"cash_payments_value": MONEY, "exp_comit_value": MONEY, "shares_value": MONEY, "aggregate_deal_value": MONEY,
              "shares_amount": SHARES}
_CURRENCY_BEFORE = re.compile(r"(?:[A-Z]{0,2}\$|\b(?:CAD|USD|AUD|EUR|GBP|NZD))\s?$")
_MONEY_AFTER = re.compile(r"\s*(?:million\s+|thousand\s+)?(?:dollars|in cash)\b", re.I)
_SHARES_AFTER = re.compile(r"\s*(?:million\s+|thousand\s+)?(?:[a-z-]+\s){0,3}(?:shares|units|warrants)\b", re.I)


def occurrence_kind(text: str, start: int, end: int) -> str:
    """What a number at text[start:end] counts: money, a share count, or neither."""
    if _CURRENCY_BEFORE.search(text[max(0, start - 5):start]) or _MONEY_AFTER.match(text[end:end + 30]):
        return MONEY
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
        patterns += [r"(?<![\d.,])" + re.escape(f) + r"\s*(?:million|mm\b|m\b)" for f in _forms(x / 1e6)]
        patterns += [r"(?<![\d.,])" + re.escape(f) + r"\s*(?:thousand|k\b)" for f in _forms(x / 1e3)]
    other = {MONEY: SHARES, SHARES: MONEY}.get(kind)
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.I):
            if other and occurrence_kind(text, match.start(), match.end()) == other:
                continue
            start, end = max(0, match.start() - 90), min(len(text), match.end() + 90)
            return re.sub(r"\s+", " ", text[start:end]).strip()
    return None


_AMOUNT = re.compile(
    r"(?P<cur>(?:[A-Z]{0,2}\$|\b(?:CAD|USD|AUD|EUR|GBP|NZD)\b)\s?)?(?<![\d.,])(?P<num>\d{1,3}(?:,\d{3})+|\d+)(?P<frac>\.\d+)?"
    r"\s*(?P<unit>million|thousand|m\b|k\b)?(?=(?P<after>[^.;\n|]{0,40}))", re.I)


def written_amounts(text: str, exclude: str | None = None) -> list[float]:
    """Amounts written in the text, with repeats. A bare number such as a year does not count:
    it needs a thousands separator, a currency sign, or a million/thousand unit.

    Each amount has a kind (money, a share count, or other) so that a sum never mixes dollars with shares;
    `exclude` drops one kind."""
    out = []
    for m in _AMOUNT.finditer(text):
        if not (m.group("cur") or "," in m.group("num") or m.group("unit")):
            continue
        value = float(m.group("num").replace(",", "") + (m.group("frac") or ""))
        unit = (m.group("unit") or "").lower()
        value *= 1e6 if unit in ("million", "m") else 1e3 if unit in ("thousand", "k") else 1
        if value < 1000:
            continue
        kind = MONEY if m.group("cur") else occurrence_kind(text, m.start("num"), m.end())
        if kind != exclude:
            out.append(value)
    return sorted(out)


def explain_by_rules(x: float, amounts: list[float], text: str, kind: str | None = None) -> str | None:
    """A sum of up to three written amounts, or a written total (or two-term sum) split across 2-6 projects.
    An amount is used twice only if it is written twice. This is a plausibility test, not a proof."""

    def as_sum(target: float, max_terms: int) -> str | None:
        candidates = [a for a in amounts if a < target]
        for n in range(2, max_terms + 1):
            for combo in itertools.combinations(candidates, n):
                if abs(sum(combo) - target) <= 0.5:
                    return " + ".join(f"{c:,.0f}" for c in combo)
        return None

    summed = as_sum(x, 3)
    if summed:
        return "sum of " + summed
    for n in range(2, 7):
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
        return sum(1 for c in self.fields.values() if c.status != EMPTY)

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


def check_schema(extraction: dict) -> list[str]:
    issues = []
    for name in CHECKED_FIELDS + ("longitude", "latitude", "owner_marketcap", "drilling_value", "shares_value_scraped"):
        value = extraction.get(name)
        if value not in (None, "") and to_number(value) is None:
            issues.append(f"{name} must be a number or null, got {value!r}")
    for name in CURRENCY_FIELDS:
        value = extraction.get(name)
        if value not in (None, "") and not re.fullmatch(r"[A-Z]{3}", str(value)):
            issues.append(f"{name} must be a 3-letter ISO code, got {value!r}")
    news_date = extraction.get("news_date")
    if news_date not in (None, ""):
        try:
            date.fromisoformat(str(news_date))
        except ValueError:
            issues.append(f"news_date must be YYYY-MM-DD, got {news_date!r}")
    for name, low, high in (("nsr_percent", 0, 100), ("interest_acquired_percent", 0, 100),
                            ("latitude", -90, 90), ("longitude", -180, 180)):
        x = to_number(extraction.get(name))
        if x is not None and not low <= x <= high:
            issues.append(f"{name} out of range: {x:g}")
    return issues


def validate(extraction: dict, article_text: str) -> Report:
    text = article_text.replace("\xa0", " ").replace("’", "'")
    money_amounts, share_amounts = written_amounts(text, exclude=SHARES), written_amounts(text, exclude=MONEY)
    report = Report(schema_issues=check_schema(extraction))
    values = {name: to_number(extraction.get(name)) for name in CHECKED_FIELDS}

    for name in CHECKED_FIELDS:
        x = values[name]
        if x is None or x == 0:
            report.fields[name] = FieldCheck(EMPTY)
            continue
        snippet = find_number(text, x, percent=name in PERCENT_FIELDS, kind=FIELD_KIND.get(name))
        report.fields[name] = FieldCheck(FOUND, x, snippet) if snippet else FieldCheck(NOT_FOUND, x)

    def supported(name: str) -> bool:
        return report.fields[name].status in (FOUND, DERIVED)

    def derive(name: str, evidence: str | None) -> None:
        if evidence and report.fields[name].status == NOT_FOUND:
            report.fields[name] = FieldCheck(DERIVED, values[name], evidence)

    # Payments and share counts are often a sum of tranches, or one total shared by several projects.
    for name in MONEY_FIELDS + ("shares_amount",):
        if report.fields[name].status == NOT_FOUND:
            amounts = share_amounts if name == "shares_amount" else money_amounts
            derive(name, explain_by_rules(values[name], amounts, text, FIELD_KIND.get(name)))

    # Exploration commitments stated in metres are converted at a fixed rate.
    if report.fields["exp_comit_value"].status == NOT_FOUND:
        metres = values["exp_comit_value"] / DRILLING_USD_PER_METRE
        if metres == int(metres) and find_number(text, metres):
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
