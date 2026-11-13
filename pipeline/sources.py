"""Where articles come from: the MiningHub listing for today's deal news, and the article page itself."""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import requests

API_URL = "https://mininghub.com/api/articles"
DEAL_TAGS = ["Mergers & Acquisitions", "Joint Venture", "Project Acquisition", "Strategic Investment"]
HEADERS = {
    "accept": "*/*", "content-type": "application/json",
    "origin": "https://mininghub.com", "referer": "https://mininghub.com/articles",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
}


def listing_payload(now: datetime) -> dict:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    stamp = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"  # noqa: E731
    return {
        "lastIds": {}, "commodities": {"data": [], "options": {"anyAll": "any"}},
        "daterange": {"startDate": stamp(start), "endDate": stamp(now), "timezoneOffset": -120, "option": "custom"},
        "deposits": {"data": [], "options": {"anyAll": "any"}}, "marketcap": [None, None],
        "nsr": {"data": ["Any"]}, "outstandingShares": [None, None], "stages": {"data": []},
        "tags": {"data": DEAL_TAGS, "options": {"anyAll": "any"}},
    }


def fetch_todays_articles(post=requests.post, now: datetime | None = None, max_pages: int = 3, pause: float = 1.0) -> list[dict]:
    """Today's deal-tagged articles, newest first. Later pages are requested by moving the end of the
    date range to just before the oldest article already seen."""
    payload = listing_payload(now or datetime.now(UTC))
    articles: list[dict] = []
    seen: set = set()
    for _ in range(max_pages):
        response = post(API_URL, headers=HEADERS, json=payload, timeout=30)
        response.raise_for_status()
        page = response.json().get("data", [])
        if not page:
            break
        for article in page:
            if article["id"] not in seen:
                seen.add(article["id"])
                articles.append(article)
        oldest = datetime.fromisoformat(page[-1]["date"].replace("Z", "+00:00"))
        payload["daterange"]["endDate"] = (oldest - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        time.sleep(pause)
    return articles


def new_since(articles_newest_first: list[dict], last_processed_id: str) -> list[dict]:
    """Articles published after the last processed one, returned oldest first so they are handled in order."""
    fresh = []
    for article in articles_newest_first:
        if str(article["id"]) == last_processed_id:
            break
        fresh.append(article)
    return fresh[::-1]


def read_state(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def write_state(path: str, article_id: str) -> None:
    with open(path, "w") as f:
        f.write(str(article_id))


class Scraper:
    """Reads an article page with a headless browser. One browser is shared across the run."""

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        return self

    def __exit__(self, *exc):
        self._browser.close()
        self._playwright.stop()

    def __call__(self, url: str) -> str | None:
        page = self._browser.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            for selector in ("article", "main"):
                target = page.locator(selector).first
                if target.count() and target.is_visible():
                    return " ".join(target.inner_text().split())
            return " ".join(page.locator("body").inner_text().split())
        except Exception:
            return None
        finally:
            page.close()
