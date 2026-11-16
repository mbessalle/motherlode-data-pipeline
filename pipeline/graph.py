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

from . import buyer, schema, telemetry
from .config import FALLBACK_TABLE
from .search import release_date
from .validate import to_number as schema_number
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
    retry_failed: bool       # the corrected retry returned nothing: the first attempt is quarantined and kept
    buyer_check: dict        # Jev's P(owner is the buyer), when configured


@dataclass
class Deps:
    scrape: Callable[[str], str | None]
    extract: Callable[[str, dict, str | None], dict]
    store: object            # PostgresStore or MemoryStore
    review: object           # ReviewQueue
    max_attempts: int = 2
    market: Callable[[str | None, str | None], dict | None] | None = None   # (ticker, date) -> close, or None
    buyer: Callable[[str, str | None, str | None], dict | None] | None = None   # (text, owner, project) -> P(buyer)


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
        with telemetry.span("extract", **{"pipeline.extract.attempt": attempts, "pipeline.extract.is_retry": attempts > 1}) as s:
            extraction = deps.extract(state["text"], state["record"], state.get("feedback"))
            failed = extraction.pop("_error", None) if isinstance(extraction, dict) else None
            if failed:
                s.set_attribute("error.type", failed)
        filled = sum(1 for value in extraction.values() if value not in (None, ""))
        telemetry.progress(state["article"].get("id"), "extract", f"attempt {attempts}: "
                           + (f"call failed ({failed})" if failed else f"{filled} fields filled")
                           + (" (retry with feedback)" if attempts > 1 else ""))
        if extraction:
            return {"extraction": extraction, "attempts": attempts}
        if attempts > 1:
            # A failed or empty retry keeps the first attempt: its supported values are written and the rest
            # withheld, instead of losing the whole row (critic F3).
            return {"attempts": attempts, "retry_failed": True}
        return {"attempts": attempts, "outcome": "extract_failed" if failed else "no_data"}

    def market_price(state: ArticleState, extraction: dict) -> tuple[dict | None, dict]:
        """The buyer's close on the news date, looked up by code (the client's share price is market data). When the
        model gave a share count but no price, the price is filled and shares value = count x price is computed."""
        if deps.market is None or not (extraction.get("share_price") or extraction.get("shares_amount")):
            return None, extraction
        day = str(extraction.get("news_date") or "")[:10] or (state["article"].get("date") or "")[:10] or None
        with telemetry.span("market_price", **{"pipeline.market.ticker": extraction.get("owner_ticker")}) as s:
            close = deps.market(extraction.get("owner_ticker"), day)
            s.set_attribute("pipeline.market.found", close is not None)
        if close and not extraction.get("share_price"):
            extraction = {**extraction, "share_price": close["close"]}
            amount = schema_number(extraction.get("shares_amount"))
            if amount and not extraction.get("shares_value"):
                extraction["shares_value"] = round(amount * close["close"], 2)
        return close, extraction

    def check(state: ArticleState) -> dict:
        close, extraction = market_price(state, state["extraction"])
        with telemetry.span("validate") as s:
            report = validate(extraction, state["text"], state["article"].get("project_count", 1), close)
            s.set_attribute("pipeline.validate.checked", report.checked)
            s.set_attribute("pipeline.validate.grounded", report.grounded)
            s.set_attribute("pipeline.validate.not_found", ",".join(report.not_found))
            s.set_attribute("pipeline.validate.passed", report.passed)
        telemetry.progress(state["article"].get("id"), "validate", f"{report.grounded} of {report.checked} numbers supported"
                           + (f"; NOT supported: {', '.join(report.not_found)}" if report.not_found else "")
                           + (f"; schema: {'; '.join(report.schema_issues)}" if report.schema_issues else ""))
        return {"report": report.as_dict(), "feedback": report.feedback(), "extraction": extraction}

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
        row = schema.normalise(state["extraction"])   # typed values, known columns only
        row["news_link"] = article["link"]
        row["news_title"] = article.get("title")
        # The listing date can trail the release by days; keep the model's date only if the announcement writes it.
        row["news_date"] = release_date(state.get("text", ""), article.get("date") or "", row.get("news_date"))
        withheld = dict(state.get("withheld") or {})
        owner_check = check_owner(state, row)
        if owner_check and owner_check["p_buyer"] < buyer.REVIEW:
            # The stored owner is probably not the buyer (the client's owner): withhold it and its ticker for review.
            gone = {k: row.pop(k) for k in ("owner_name", "owner_ticker", "owner_marketcap") if row.get(k)}
            withheld.update(gone)
            deps.review.add(article, state.get("table"), {"buyer_check": owner_check}, gone)
        suffix = "_partial" if withheld else ""
        with telemetry.span("store", **{"db.collection.name": state["table"], "pipeline.store.mode": state["status"]}) as s:
            try:
                if state["status"] == "new":
                    deps.store.insert(state["table"], {**row, "project_name": article["project_name"]})
                else:
                    deps.store.update(state["table"], article["project_name"], row)
                written = sum(1 for value in row.values() if value not in (None, ""))
                telemetry.progress(article.get("id"), "store", f"{'insert into' if state['status'] == 'new' else 'update'} "
                                   f"{state['table']}: {written} fields")
                return {"outcome": ("inserted" if state["status"] == "new" else "updated") + suffix, "withheld": withheld,
                        "buyer_check": owner_check}
            except Exception as error:
                s.record_exception(error)
                return {"outcome": "db_error"}

    def check_owner(state: ArticleState, row: dict) -> dict | None:
        """Jev's probability that the owner is the buyer, or None (no check configured, no owner, or a failed call:
        then the owner is stored unchecked, never guessed)."""
        if deps.buyer is None or not row.get("owner_name"):
            return None
        with telemetry.span("buyer_check") as s:
            try:
                result = deps.buyer(state.get("text", ""), row["owner_name"], state["article"].get("project_name"))
            except Exception as error:
                s.record_exception(error)
                s.set_attribute("error.type", type(error).__name__)
                return None
            s.set_attribute("pipeline.buyer.p", result["p_buyer"])
        telemetry.progress(state["article"].get("id"), "buyer", f"{row['owner_name']}: P(buyer) {result['p_buyer']:.2f}, "
                           f"role {result['role']}")
        return result

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
    def after_extract(state: ArticleState) -> str:
        if state.get("outcome"):
            return END
        return "quarantine" if state.get("retry_failed") else "validate"

    graph.add_conditional_edges("extract", after_extract, ["validate", "quarantine", END])
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
