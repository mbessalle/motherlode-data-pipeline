"""The per-article workflow as a LangGraph state machine.

    resolve ──► scrape ──► extract ──► validate ──┬─ all values supported ──► store ──► END
       │           │          ▲                    ├─ unsupported, attempts left ─► extract (with feedback)
       ▼           ▼          └────────────────────┘
      END         END                              └─ unsupported, no attempts left ─► quarantine ──► store

A graph, not a straight script, because an article can leave the path at four points and loop at one:
it may have no project or link (resolve), no readable page (scrape), nothing extracted (extract),
and values the article does not support (validate), which earn one corrected retry and are then withheld
from the database and queued for a person, while the supported values are still written.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from . import telemetry
from .config import FALLBACK_TABLE
from .validate import validate


class ArticleState(TypedDict, total=False):
    article: dict            # the listing entry, with its `classification`
    table: str
    status: str              # "new" or "existing"
    record: dict             # the existing row, or {"project_name": ...}
    text: str
    extraction: dict
    report: dict             # validate.Report.as_dict()
    feedback: str
    attempts: int
    withheld: dict           # values removed before writing
    outcome: str             # how the article left the graph


@dataclass
class Deps:
    scrape: Callable[[str], str | None]
    extract: Callable[[str, dict, str | None], dict]
    store: object            # PostgresStore or MemoryStore
    review: object           # ReviewQueue
    max_attempts: int = 2


def build_graph(deps: Deps):
    def resolve(state: ArticleState) -> dict:
        article = state["article"]
        name, link = article.get("project_name"), article.get("link")
        if not name:
            return {"outcome": "skipped_no_project"}
        if not link:
            return {"outcome": "skipped_no_link"}
        classification = article.get("classification", {})
        # An article whose continent could not be determined goes to the catch-all table.
        table = FALLBACK_TABLE if classification.get("status") == "unclassified" else classification.get("table_name")
        if not table:
            return {"outcome": "skipped_no_table"}
        existing = deps.store.fetch_record(table, name)
        return {"table": table, "status": "existing" if existing else "new",
                "record": existing or {"project_name": name}, "attempts": 0}

    def scrape(state: ArticleState) -> dict:
        with telemetry.span("scrape", **{"url.full": state["article"]["link"]}) as s:
            text = deps.scrape(state["article"]["link"])
            s.set_attribute("pipeline.article.chars", len(text or ""))
        return {"text": text} if text else {"outcome": "skipped_no_content"}

    def extract(state: ArticleState) -> dict:
        attempts = state.get("attempts", 0) + 1
        with telemetry.span("extract", **{"pipeline.extract.attempt": attempts, "pipeline.extract.is_retry": attempts > 1}):
            extraction = deps.extract(state["text"], state["record"], state.get("feedback"))
        filled = sum(1 for value in extraction.values() if value not in (None, ""))
        telemetry.progress(state["article"].get("id"), "extract", f"attempt {attempts}: {filled} fields filled"
                           + (" (retry with feedback)" if attempts > 1 else ""))
        return {"extraction": extraction, "attempts": attempts} if extraction else {"attempts": attempts, "outcome": "no_data"}

    def check(state: ArticleState) -> dict:
        with telemetry.span("validate") as s:
            report = validate(state["extraction"], state["text"])
            s.set_attribute("pipeline.validate.checked", report.checked)
            s.set_attribute("pipeline.validate.grounded", report.grounded)
            s.set_attribute("pipeline.validate.not_found", ",".join(report.not_found))
            s.set_attribute("pipeline.validate.passed", report.passed)
        telemetry.progress(state["article"].get("id"), "validate", f"{report.grounded} of {report.checked} numbers supported"
                           + (f"; NOT supported: {', '.join(report.not_found)}" if report.not_found else "")
                           + (f"; schema: {'; '.join(report.schema_issues)}" if report.schema_issues else ""))
        return {"report": report.as_dict(), "feedback": report.feedback()}

    def quarantine(state: ArticleState) -> dict:
        """Withhold what the article does not support, queue it for review, keep the rest."""
        report, extraction = state["report"], dict(state["extraction"])
        withheld = {name: extraction.get(name) for name in report["not_found"]}
        for name in withheld:
            extraction[name] = None
        deps.review.add(state["article"], state.get("table"), report, withheld)
        if report["schema_issues"]:  # a malformed record is not written at all
            return {"withheld": withheld, "outcome": "review_only"}
        return {"extraction": extraction, "withheld": withheld}

    def store(state: ArticleState) -> dict:
        article = state["article"]
        row = dict(state["extraction"])
        row["news_link"] = article["link"]
        row["news_title"] = article.get("title")
        if article.get("date"):
            row["news_date"] = article["date"].split("T")[0]
        suffix = "_partial" if state.get("withheld") else ""
        with telemetry.span("store", **{"db.collection.name": state["table"], "pipeline.store.mode": state["status"]}) as s:
            try:
                if state["status"] == "new":
                    deps.store.insert(state["table"], {"project_name": article["project_name"], **row})
                else:
                    deps.store.update(state["table"], article["project_name"], row)
                written = sum(1 for value in row.values() if value not in (None, ""))
                telemetry.progress(article.get("id"), "store", f"{'insert into' if state['status'] == 'new' else 'update'} "
                                   f"{state['table']}: {written} fields")
                return {"outcome": ("inserted" if state["status"] == "new" else "updated") + suffix}
            except Exception as error:
                s.record_exception(error)
                return {"outcome": "db_error"}

    def after(step_if_ok: str):
        return lambda state: END if state.get("outcome") else step_if_ok

    def after_check(state: ArticleState) -> str:
        if state["report"]["passed"]:
            return "store"
        return "extract" if state["attempts"] < deps.max_attempts else "quarantine"

    graph = StateGraph(ArticleState)
    for name, node in (("resolve", resolve), ("scrape", scrape), ("extract", extract), ("validate", check),
                       ("quarantine", quarantine), ("store", store)):
        graph.add_node(name, node)
    graph.add_edge(START, "resolve")
    graph.add_conditional_edges("resolve", after("scrape"), ["scrape", END])
    graph.add_conditional_edges("scrape", after("extract"), ["extract", END])
    graph.add_conditional_edges("extract", after("validate"), ["validate", END])
    graph.add_conditional_edges("validate", after_check, ["store", "extract", "quarantine"])
    graph.add_conditional_edges("quarantine", after("store"), ["store", END])
    graph.add_edge("store", END)
    return graph.compile()


def process_article(app, article: dict) -> ArticleState:
    """Run one article through the graph inside one trace."""
    with telemetry.span("process_article", **{"pipeline.article.id": str(article.get("id")),
                                              "pipeline.project": article.get("project_name")}) as s:
        final = app.invoke({"article": article})
        s.set_attribute("pipeline.outcome", final.get("outcome", "unknown"))
        s.set_attribute("pipeline.extract.attempts", final.get("attempts", 0))
        return final
