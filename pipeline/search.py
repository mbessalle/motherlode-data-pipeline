"""Find the original announcement for a headline, and read it.

A search result is accepted on its own evidence, never on trust: the page must be long enough to be a
release, name the company, and share the headline's distinctive words (numbers and capitalised names).
The listing site rewrites headlines, so the test is on distinctive words, not on the exact title.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import requests

SERPER_URL = "https://google.serper.dev/search"
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
SKIP_HOSTS = ("mininghub.com", "facebook.com", "twitter.com", "x.com", "linkedin.com", "youtube.com", "reddit.com",
              "stocktwits.com", "hotcopper.com.au")
MIN_CHARS = 1200
_COMPANY_NOISE = {"ltd", "limited", "inc", "corp", "corporation", "plc", "nl", "co", "company", "resources", "mining",
                  "minerals", "metals", "gold", "group", "holdings", "the"}
_TITLE_NOISE = {"the", "for", "and", "with", "from", "into", "its", "has", "have", "will", "new", "first",
                # words every mining headline shares: they identify nothing
                "gold", "silver", "copper", "lithium", "uranium", "nickel", "zinc", "project", "property", "mine", "claims",
                "minerals", "mining", "metals", "resources", "shares", "deal", "acquisition", "sale", "option", "agreement",
                # headline verbs are the listing site's wording, not the release's
                "acquires", "acquire", "agrees", "completes", "closes", "secures", "signs", "sells", "divests", "options",
                "announces", "enters", "clears", "approve", "approves", "locks", "buys", "takes", "wins", "sets"}
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November",
           "December"]
# The listing site publishes its item up to two weeks after the release it reports.
DAYS_BEFORE, DAYS_AFTER = 14, 1


@dataclass
class Source:
    url: str
    text: str
    how: str


def company_words(company: str) -> list[str]:
    """The words that identify a company: 'breakthrough minerals limited' -> ['breakthrough']."""
    words = [w for w in re.findall(r"[a-z0-9]+", company.lower()) if w not in _COMPANY_NOISE]
    return words or re.findall(r"[a-z0-9]+", company.lower())[:1]


def distinctive_words(title: str) -> set[str]:
    """Numbers and capitalised names in a headline, lower-cased: the parts a rewrite keeps.
    'Fuse Options Foggy Mountain Copper-Gold Project in BC' -> {fuse, foggy, mountain}."""
    words = set()
    for token in re.findall(r"[A-Za-z][A-Za-z']+|\d[\d.,]*\d|\d", title):
        bare = token.strip(".,").lower()
        if token[0].isdigit():
            words.add(bare.replace(",", ""))
        elif token[0].isupper() and len(bare) > 2 and bare not in _TITLE_NOISE:
            words.add(bare)
    return words


def date_spellings(day: date) -> list[str]:
    """How a release writes its date: November 12, 2026 / Nov. 12, 2026 / 12 November 2026 / 2026-11-12."""
    month, short = _MONTHS[day.month - 1], _MONTHS[day.month - 1][:3]
    return [rf"{month} {day.day}(?:st|nd|rd|th)?,? {day.year}", rf"{short}\.? {day.day}(?:st|nd|rd|th)?,? {day.year}",
            rf"{day.day}(?:st|nd|rd|th)? {month},? {day.year}", rf"{day.day} {short}\.? {day.year}", day.isoformat()]


def dated_near(text: str, listed: str) -> bool:
    """Is a date within a few days of the listing date written in the text? Guards against an older release
    or an unrelated document that happens to name the same company and project."""
    try:
        listed_day = datetime.fromisoformat(listed.replace("Z", "+00:00")).date()
    except ValueError:
        return True  # no usable listing date: do not block on it
    for offset in range(-DAYS_BEFORE, DAYS_AFTER + 1):
        spellings = date_spellings(listed_day + timedelta(days=offset))
        if any(re.search(r"(?<!\d)" + spelling + r"(?!\d)", text, re.I) for spelling in spellings):
            return True
    return False


def release_date(text: str, listed: str, model_date) -> str | None:
    """The date to store for an announcement.

    The model's date is used only when it is a real date, lies within the window around the listing date and is
    written in the announcement. Otherwise the listing date is used. A date is never picked from the text by
    position: releases routinely open by citing earlier releases ("further to its news releases dated ...")."""
    listed_day = listed.split("T")[0] if listed else None
    try:
        claimed = date.fromisoformat(str(model_date))
        window_start = date.fromisoformat(listed_day) - timedelta(days=DAYS_BEFORE) if listed_day else None
        window_end = date.fromisoformat(listed_day) + timedelta(days=DAYS_AFTER) if listed_day else None
    except (TypeError, ValueError):
        return listed_day
    if window_start and not window_start <= claimed <= window_end:
        return listed_day
    written = any(re.search(r"(?<!\d)" + spelling + r"(?!\d)", text or "", re.I) for spelling in date_spellings(claimed))
    return claimed.isoformat() if written else listed_day


def is_the_announcement(text: str, title: str, company: str, listed: str = "", min_share: float = 0.7) -> tuple[bool, str]:
    if not text or len(text) < MIN_CHARS:
        return False, "too short"
    low = re.sub(r"[^a-z0-9.]+", " ", text.lower())
    missing_company = [w for w in company_words(company) if w not in low]
    if missing_company:
        return False, f"company not named ({', '.join(missing_company)})"
    wanted = distinctive_words(title)
    found = {w for w in wanted if w in low or w.replace(".", "") in low.replace(",", "")}
    share = len(found) / max(1, len(wanted))
    verdict = f"{len(found)} of {len(wanted)} headline names and numbers"
    if share < min_share:
        return False, verdict
    if listed and not dated_near(text, listed):
        return False, verdict + ", but not dated near the listing date"
    return True, verdict


def read_page(url: str, get=requests.get) -> tuple[str | None, str]:
    """Text of a page or PDF, or (None, reason)."""
    try:
        response = get(url, headers=BROWSER_UA, timeout=30)
        if response.status_code != 200:
            return None, f"http {response.status_code}"
        if "pdf" in response.headers.get("content-type", "") or url.lower().split("?")[0].endswith(".pdf"):
            from pypdf import PdfReader

            pages = PdfReader(io.BytesIO(response.content)).pages
            return "\n".join(page.extract_text() or "" for page in pages), "pdf"
        import trafilatura

        return trafilatura.extract(response.text, include_tables=True, favor_recall=True), "html"
    except Exception as error:
        return None, type(error).__name__


class Searcher:
    def __init__(self, api_key: str, post=requests.post, get=requests.get, max_candidates: int = 8):
        self.api_key, self.post, self.get, self.max_candidates = api_key, post, get, max_candidates
        self.searches = 0
        self.on_candidate = None   # optional callback(article, url, accepted, reason), used for live progress

    def results(self, query: str) -> list[dict]:
        self.searches += 1
        response = self.post(SERPER_URL, json={"q": query, "num": 10}, headers={"X-API-KEY": self.api_key}, timeout=30)
        response.raise_for_status()
        return response.json().get("organic", [])

    def find_announcement(self, article: dict) -> tuple[Source | None, list[dict]]:
        """One search for the headline and company; the first result that passes the identity test wins.
        Also returns what was tried, for the run report."""
        tried = []
        for result in self.results(f'{article["title"]} {article["company"]}')[: self.max_candidates]:
            url = result.get("link", "")
            if not url or any(host in url for host in SKIP_HOSTS):
                continue
            text, how = read_page(url, self.get)
            ok, reason = is_the_announcement(text or "", article["title"], article["company"], article.get("date", ""))
            tried.append({"url": url, "read": how, "verdict": reason})
            if self.on_candidate:
                self.on_candidate(article, url, ok, reason)
            if ok:
                return Source(url, " ".join(text.split()), how), tried
        return None, tried
