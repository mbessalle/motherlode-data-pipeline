"""One scheduled run.

    news feed ─► headline filter ─► search for the announcement ─► triage (project deal? which project?)
              ─► project name must be written in the announcement ─► classify region ─► per-article graph
"""
from __future__ import annotations

import collections
import functools
import json
import re

from . import config, llm, sources, telemetry
from .graph import Deps, build_graph, process_article
from .search import Searcher
from .store import PostgresStore, ReviewQueue

STATE_FILE = "last_article_id.txt"
DESCRIPTION_CHARS = 600


def classify(article: dict, locate, store) -> dict:
    """Attach the target table and whether the project is already in it."""
    context = (f"tags: '{article.get('tags', '')}', title: '{article.get('title', '')}', "
               f"description: '{article.get('description', '')}'")
    location = locate(context)
    continent = location.get("continent")
    result = {"country": location.get("country"), "continent": continent, "table_name": None, "status": "unclassified"}
    table = config.CONTINENT_TO_TABLE.get(continent)
    if table:
        exists = store.project_exists(table, article["project_name"])
        result.update(table_name=table, status="existing" if exists else "new",
                      reason=f"Project {'found' if exists else 'not found'} in table '{table}'.")
    else:
        result["reason"] = "No continent determined." if not continent else f"Invalid continent: '{continent}'."
    return result


def named_in(text: str, name: str) -> bool:
    def squash(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

    return bool(name) and squash(name) in squash(text)


def prepare(article: dict, searcher, triage) -> tuple[str | None, dict]:
    """Find and screen the announcement behind a headline. Returns (reason it stops here | None, details)."""
    with telemetry.span("find_announcement", **{"pipeline.article.id": str(article["id"])}) as s:
        source, tried = searcher.find_announcement(article)
        s.set_attribute("pipeline.search.candidates", len(tried))
        s.set_attribute("pipeline.search.found", source is not None)
    details = {"search_tried": tried}
    if source is None:
        return "source_not_found", details
    details["source_url"] = source.url
    verdict = triage(article["title"], article["company"], source.text)
    details["triage"] = verdict
    if not verdict["is_project_deal"]:
        return "not_a_project_deal", details
    if not named_in(source.text, verdict["project_name"] or ""):
        return "project_name_not_in_source", details
    article.update(project_name=verdict["project_name"], link=source.url, text=source.text,
                   description=source.text[:DESCRIPTION_CHARS], tags="")
    return None, details


def run(settings=None, store=None, client=None, searcher=None, fetch=sources.fetch_news) -> dict:
    """Returns a summary: how many headlines were looked at, and how each candidate ended."""
    settings = settings or config.load()
    telemetry.setup()
    if client is None:
        import openai

        client = openai.OpenAI(api_key=settings.openai_api_key)
    searcher = searcher or Searcher(settings.serper_api_key)
    if getattr(searcher, "on_candidate", "absent") is None:
        searcher.on_candidate = lambda article, url, ok, reason: telemetry.progress(
            article["id"], "search", f"{'ACCEPT' if ok else 'reject'} {url[:90]} ({reason})")

    with telemetry.span("pipeline_run") as run_span:
        new_articles = sources.new_since(fetch(), sources.read_state(STATE_FILE))
        run_span.set_attribute("pipeline.articles.new", len(new_articles))
        if not new_articles:
            print("[pipeline] No new articles.")
            return {"articles": 0, "candidates": 0, "outcomes": {}}

        owns_store = store is None
        store = store or PostgresStore.connect(settings)
        texts: dict[str, str] = {}
        try:
            deps = Deps(
                scrape=texts.get,   # the announcement was already read while finding it
                extract=functools.partial(llm.extract_deal, client, settings.extract_model),
                store=store, review=ReviewQueue(settings.review_queue_path), max_attempts=settings.max_extract_attempts,
            )
            app = build_graph(deps)
            locate = functools.partial(llm.classify_location, client, settings.classify_model)
            triage = functools.partial(llm.triage, client, settings.classify_model)
            outcomes: collections.Counter = collections.Counter()
            report, candidates, looked_at, last_id = [], 0, 0, None
            for article in new_articles:                      # oldest first
                if settings.max_articles and candidates >= settings.max_articles:
                    break                                     # the rest are picked up by the next run
                looked_at += 1
                last_id = article["id"]
                if not sources.looks_like_a_deal(article["title"]):
                    continue
                candidates += 1
                telemetry.progress(article["id"], "headline", f"{article['title'][:90]} [{article['company']}]")
                stop, details = prepare(article, searcher, triage)
                if details.get("triage"):
                    verdict = details["triage"]
                    kind = "project deal" if verdict["is_project_deal"] else "not a project deal"
                    telemetry.progress(article["id"], "triage",
                                       f"{kind}; project {verdict['project_name']!r}; {verdict['reason']}")
                final: dict = {}
                if stop is None:
                    texts[article["link"]] = article.pop("text")
                    article["classification"] = classify(article, locate, store)
                    telemetry.progress(article["id"], "region", f"{article['classification']['continent']} -> table "
                                       f"{article['classification']['table_name']} ({article['classification']['status']})")
                    final = process_article(app, article)
                outcome = stop or final.get("outcome", "unknown")
                outcomes[outcome] += 1
                telemetry.progress(article["id"], "RESULT", outcome
                                   + (f"; project {article.get('project_name')!r}" if article.get("project_name") else "")
                                   + (f"; withheld: {', '.join(final['withheld'])}" if final.get("withheld") else ""))
                report.append({
                    "id": article["id"], "headline": article["title"], "company": article["company"], "date": article["date"],
                    "listing_link": article["listing_link"], **details,
                    "project_name": article.get("project_name"), "classification": article.get("classification"),
                    "table": final.get("table"), "status": final.get("status"), "outcome": outcome,
                    "attempts": final.get("attempts", 0), "article_chars": len(final.get("text") or ""),
                    "extraction": final.get("extraction"), "withheld": final.get("withheld"), "validation": final.get("report"),
                })
        finally:
            if owns_store:
                store.close()

        # Only after the articles were handled is progress recorded, so a crashed run is retried in full.
        sources.write_state(STATE_FILE, str(last_id))
        with open(settings.run_report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=1, default=str)   # what each candidate produced, for a person to inspect
        run_span.set_attribute("pipeline.candidates", candidates)
        run_span.set_attribute("pipeline.searches", getattr(searcher, "searches", 0))
        run_span.set_attribute("pipeline.outcomes", ", ".join(f"{k}={v}" for k, v in sorted(outcomes.items())))
        return {"articles": looked_at, "candidates": candidates, "outcomes": dict(outcomes)}
