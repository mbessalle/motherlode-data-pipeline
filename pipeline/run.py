"""One scheduled run: fetch today's deal news, classify each article, process it through the graph."""
from __future__ import annotations

import collections
import functools

from . import config, llm, sources, telemetry
from .graph import Deps, build_graph, process_article
from .store import PostgresStore, ReviewQueue

STATE_FILE = "last_article_id.txt"


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


def run(settings=None, store=None, scrape=None, client=None, fetch=sources.fetch_todays_articles) -> dict:
    """Returns a summary: how many articles, and how each one left the graph."""
    settings = settings or config.load()
    telemetry.setup()
    if client is None:
        import openai

        client = openai.OpenAI(api_key=settings.openai_api_key)

    with telemetry.span("pipeline_run") as run_span:
        last_id = sources.read_state(STATE_FILE)
        articles = sources.new_since(fetch(), last_id)
        run_span.set_attribute("pipeline.articles.new", len(articles))
        if not articles:
            print("[pipeline] No new articles.")
            return {"articles": 0, "outcomes": {}}

        owns_store = store is None
        store = store or PostgresStore.connect(settings)
        scraper_context = None
        if scrape is None:
            scraper_context = sources.Scraper()
            scrape = scraper_context.__enter__()
        try:
            deps = Deps(
                scrape=scrape,
                extract=functools.partial(llm.extract_deal, client, settings.extract_model),
                store=store,
                review=ReviewQueue(settings.review_queue_path),
                max_attempts=settings.max_extract_attempts,
            )
            app = build_graph(deps)
            locate = functools.partial(llm.classify_location, client, settings.classify_model)
            outcomes: collections.Counter = collections.Counter()
            for article in articles:
                if article.get("project_name"):
                    article["classification"] = classify(article, locate, store)
                final = process_article(app, article)
                outcome = final.get("outcome", "unknown")
                outcomes[outcome] += 1
                print(f"[pipeline] {article.get('id')} {article.get('project_name')!r}: {outcome}"
                      + (f" (withheld: {', '.join(final['withheld'])})" if final.get("withheld") else ""))
        finally:
            if scraper_context is not None:
                scraper_context.__exit__(None, None, None)
            if owns_store:
                store.close()

        # Only after every article was handled is the newest id recorded, so a crashed run is retried in full.
        sources.write_state(STATE_FILE, str(articles[-1]["id"]))
        run_span.set_attribute("pipeline.outcomes", ", ".join(f"{k}={v}" for k, v in sorted(outcomes.items())))
        return {"articles": len(articles), "outcomes": dict(outcomes)}
