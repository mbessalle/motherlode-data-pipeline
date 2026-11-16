"""One scheduled run.

    news feed ─► headline filter ─► search for the announcement ─► triage (project deal? which project?)
              ─► project name must be written in the announcement ─► classify region ─► per-article graph
"""
from __future__ import annotations

import collections
import functools
import json
import os
import re

from . import buyer, config, llm, market, sources, telemetry
from .graph import Deps, build_graph, process_article
from .search import Searcher
from .store import PostgresStore, ReviewQueue

STATE_FILE = "last_article_id.txt"
FAILURES_FILE = "failed_articles.json"   # tries per article id that failed for a temporary reason
MAX_TRIES = 3                            # after this many failed runs an article is reported and passed
TEMPORARY_FAILURES = ("triage_failed", "classify_failed", "extract_failed", "db_error")
DESCRIPTION_CHARS = 600


def classify(article: dict, locate, store) -> dict:
    """Attach the target table and whether the project is already in it."""
    context = (f"tags: '{article.get('tags', '')}', title: '{article.get('title', '')}', "
               f"description: '{article.get('description', '')}'")
    location = locate(context)
    if location.get("error"):                         # a failed call, not "no continent" (critic N3)
        return {"country": None, "continent": None, "table_name": None, "status": "classify_failed",
                "error": location["error"]}
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


def read_failures() -> dict[str, int]:
    try:
        with open(FAILURES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def write_failures(failures: dict[str, int]) -> None:
    with open(FAILURES_FILE, "w") as f:
        json.dump(failures, f)


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
    if verdict.get("error"):
        return "triage_failed", details              # not a "no": the run stops here and the next run retries it
    if not verdict["is_project_deal"]:
        return "not_a_project_deal", details
    if not verdict["project_name"] and not verdict.get("projects"):
        return "no_single_project", details          # a deal with neither a project name nor a list of projects
    projects = project_parts(verdict, source.text)
    details["projects_checked"] = projects
    if not projects:
        return "project_name_not_in_source", details
    # A group with no name of its own ("the Telfer operation and the Havieron project") is named after its projects.
    group = verdict["project_name"] or " and ".join(projects)
    article.update(project_name=group, projects=projects, link=source.url, text=source.text,
                   description=source.text[:DESCRIPTION_CHARS], tags="")
    return None, details


def project_parts(verdict: dict, text: str) -> list[str]:
    """The projects a deal covers, one row each (client rule: one row per project, shared values split equally).
    Which projects a deal covers is a judgment about the text, so the triage model gives the list; code only checks
    what is exact: every name must be written in the announcement. If any listed name is not, the list is not
    trusted and the deal stays one row under its group name. Empty when no name is written at all."""
    name, listed = verdict.get("project_name") or "", []
    for p in verdict.get("projects") or []:              # no repeats, compared case-insensitively (critic N9)
        if p and p.lower() not in (q.lower() for q in listed):
            listed.append(p)
    contained = any(a != b and a.lower() in b.lower() for a in listed for b in listed)
    if listed and not contained and all(named_in(text, p) for p in listed):
        return listed
    return [name] if named_in(text, name) else []


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
        failures = read_failures()
        try:
            deps = Deps(
                scrape=texts.get,   # the announcement was already read while finding it
                extract=functools.partial(llm.extract_deal, client, settings.extract_model),
                store=store, review=ReviewQueue(settings.review_queue_path), max_attempts=settings.max_extract_attempts,
                market=market.close_on if market.available() and os.getenv("PIPELINE_MARKET_LOOKUP", "1") != "0" else None,
                # Jev's buyer check, when an OpenRouter key is set (about 0.00004 USD a deal)
                buyer=buyer.check if os.getenv("OPENROUTER_API_KEY") and os.getenv("PIPELINE_BUYER_CHECK", "1") != "0" else None,
            )
            app = build_graph(deps)
            locate = functools.partial(llm.classify_location, client, settings.classify_model)
            triage = functools.partial(llm.triage, client, settings.classify_model)
            outcomes: collections.Counter = collections.Counter()
            report, candidates, looked_at, last_id = [], 0, 0, None
            for article in new_articles:                      # oldest first
                if settings.max_articles and candidates >= settings.max_articles:
                    break                                     # the rest are picked up by the next run
                previous_id, last_id = last_id, article["id"]
                looked_at += 1
                if not sources.looks_like_a_deal(article["title"]):
                    continue
                candidates += 1
                telemetry.progress(article["id"], "headline", f"{article['title'][:90]} [{article['company']}]")
                stop, details = prepare(article, searcher, triage)
                if details.get("triage"):
                    verdict = details["triage"]
                    kind = ("triage call FAILED: " + verdict["error"]) if verdict.get("error") else \
                        "project deal" if verdict["is_project_deal"] else "not a project deal"
                    telemetry.progress(article["id"], "triage",
                                       f"{kind}; project {verdict['project_name']!r}; {verdict['reason']}")
                base = {"id": article["id"], "headline": article["title"], "company": article["company"],
                        "date": article["date"], "listing_link": article["listing_link"], **details}
                entries: list[dict] = []
                if stop is not None:
                    entries.append({**base, "project_name": article.get("project_name"), "outcome": stop})
                else:
                    texts[article["link"]] = article.pop("text")
                    projects = article.pop("projects")
                for project in (projects if stop is None else []):   # one row per project; shared values split in N
                    one = {**article, "project_name": project, "project_count": len(projects)}
                    one["classification"] = classify(one, locate, store)
                    telemetry.progress(one["id"], "region", f"{one['classification']['continent']} -> table "
                                       f"{one['classification']['table_name']} ({one['classification']['status']})")
                    final = {"outcome": "classify_failed"} if one["classification"]["status"] == "classify_failed" \
                        else process_article(app, one)
                    outcome = final.get("outcome", "unknown")
                    text = final.get("text") or ""
                    entries.append({
                        **base, "project_name": project, "group_name": article["project_name"] if len(projects) > 1 else None,
                        "project_count": len(projects), "classification": one["classification"],
                        "table": final.get("table"), "status": final.get("status"), "outcome": outcome,
                        "attempts": final.get("attempts", 0), "article_chars": len(text),
                        "text_cut": len(text) > llm.ARTICLE_CHAR_LIMIT,   # the model saw a deal-centred window
                        "extraction": final.get("extraction"), "withheld": final.get("withheld"),
                        "buyer_check": final.get("buyer_check"),
                        "validation": final.get("report"),
                    })
                # A temporary failure (a failed model call, a database error) holds progress before this article,
                # so the next run tries it again; after MAX_TRIES runs it is reported as failed for good and passed,
                # so one bad article cannot block every later run (critic N2, N3).
                failed = any(e["outcome"] in TEMPORARY_FAILURES for e in entries)
                tries = failures.pop(str(article["id"]), 0) + 1 if failed else 0
                if failed and tries >= MAX_TRIES:
                    for e in entries:
                        if e["outcome"] in TEMPORARY_FAILURES:
                            e["outcome"] += f"_after_{tries}_tries"
                for e in entries:
                    outcomes[e["outcome"]] += 1
                    telemetry.progress(article["id"], "RESULT", f"{e['outcome']}; project {e.get('project_name')!r}"
                                       + (f"; withheld: {', '.join(e['withheld'])}" if e.get("withheld") else ""))
                    report.append(e)
                if failed and tries < MAX_TRIES:
                    failures[str(article["id"])] = tries
                    last_id = previous_id
                    break
        finally:
            if owns_store:
                store.close()

        # Only after the articles were handled is progress recorded, so a crashed run is retried in full.
        if last_id is not None:                       # nothing handled (the first triage failed): state stays as it was
            sources.write_state(STATE_FILE, str(last_id))
        write_failures(failures)
        with open(settings.run_report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=1, default=str)   # what each candidate produced, for a person to inspect
        run_span.set_attribute("pipeline.candidates", candidates)
        run_span.set_attribute("pipeline.searches", getattr(searcher, "searches", 0))
        run_span.set_attribute("pipeline.outcomes", ", ".join(f"{k}={v}" for k, v in sorted(outcomes.items())))
        return {"articles": looked_at, "candidates": candidates, "outcomes": dict(outcomes)}
