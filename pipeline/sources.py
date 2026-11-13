"""Discovery: which announcements are new.

The list comes from MiningHub's published news sitemap, the feed the site offers to automated readers
(its robots.txt advertises it and disallows the JSON API this pipeline used before). The sitemap gives a
headline, a date and the company; the announcement itself is then found by web search (pipeline/search.py).
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import requests

NEWS_SITEMAP_URL = "https://mininghub.com/news.xml"
USER_AGENT = "motherlode-data-pipeline (+https://github.com/mbessalle/motherlode-data-pipeline)"
_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9", "news": "http://www.google.com/schemas/sitemap-news/0.9"}
_LINK = re.compile(r"/company/(?P<company>[^/]+)/[^/]+/(?P<slug>.+)-(?P<id>\d+)$")

# Headline words that suggest a project-level transaction. Deliberately generous: this only decides
# which headlines are worth a search; the model decides afterwards on the announcement itself.
DEAL_WORDS = re.compile(
    r"acqui|divest|\bsells?\b|\bsale\b|\bsold\b|purchas|\boption|earn[- ]in|joint venture|\bjv\b|farm[- ]?in|farm[- ]?out|"
    r"merger|\bmerge|takeover|arrangement|consolidat|\bstake\b|\binterest in\b|agreement to", re.I)


def parse_news_sitemap(xml_text: str) -> list[dict]:
    """Sitemap entries as articles, newest first."""
    articles = []
    for url in ET.fromstring(xml_text).findall("sm:url", _NS):
        link = (url.findtext("sm:loc", default="", namespaces=_NS) or "").strip()
        match = _LINK.search(link)
        if not match:
            continue
        articles.append({
            "id": int(match.group("id")),
            "title": (url.findtext("news:news/news:title", default="", namespaces=_NS) or "").strip(),
            "date": (url.findtext("news:news/news:publication_date", default="", namespaces=_NS) or "").strip(),
            "listing_link": link,
            "company": match.group("company").replace("-", " ").strip(),
        })
    return sorted(articles, key=lambda a: (a["date"], a["id"]), reverse=True)


def fetch_news(get=requests.get) -> list[dict]:
    response = get(NEWS_SITEMAP_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    response.raise_for_status()
    return parse_news_sitemap(response.text)


def looks_like_a_deal(title: str) -> bool:
    return bool(DEAL_WORDS.search(title or ""))


def new_since(articles_newest_first: list[dict], last_processed_id: str) -> list[dict]:
    """Articles published after the last processed one, returned oldest first so they are handled in order.
    If that article has dropped out of the feed, everything in the feed counts as new."""
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
