"""Every way an article can leave the graph, with the model and the browser replaced by plain functions."""
import json

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from pipeline import telemetry
from pipeline.graph import Deps, build_graph, process_article
from pipeline.store import MemoryStore, ReviewQueue

ARTICLE_TEXT = ("NewCo will pay $100,000 in cash and issue 1,000,000 shares to acquire a 100% interest in the Acme "
                "project. The vendor keeps a 2% NSR.")
GOOD = {"cash_payments_value": 100000, "shares_amount": 1000000, "interest_acquired_percent": 100, "nsr_percent": 2,
        "deal_type": "sale"}


def article(**overrides):
    base = {"id": 1, "project_name": "Acme", "link": "https://example.test/acme", "title": "NewCo buys Acme",
            "date": "2025-03-01T10:00:00Z", "classification": {"status": "new", "table_name": "north_america"}}
    return {**base, **overrides}


@pytest.fixture
def harness(tmp_path):
    class Harness:
        def __init__(self):
            self.store = MemoryStore({"north_america": [], "other": []})
            self.queue_path = tmp_path / "review.jsonl"
            self.extractions = [GOOD]
            self.calls = []
            self.text = ARTICLE_TEXT

        def extract(self, text, record, feedback):
            self.calls.append(feedback)
            return self.extractions[min(len(self.calls) - 1, len(self.extractions) - 1)]

        def run(self, art, max_attempts=2):
            deps = Deps(scrape=lambda url: self.text, extract=self.extract, store=self.store,
                        review=ReviewQueue(str(self.queue_path)), max_attempts=max_attempts)
            return process_article(build_graph(deps), art)

        def queued(self):
            return [json.loads(line) for line in self.queue_path.read_text().splitlines()] if self.queue_path.exists() else []

    return Harness()


def test_supported_extraction_is_inserted_with_article_metadata(harness):
    final = harness.run(article())
    assert final["outcome"] == "inserted" and final["attempts"] == 1
    row = harness.store.tables["north_america"][0]
    assert row["project_name"] == "Acme" and row["cash_payments_value"] == 100000
    assert row["news_link"] == "https://example.test/acme" and row["news_date"] == "2025-03-01"   # listing date: none extracted


def test_release_date_from_the_announcement_replaces_the_listing_date(harness):
    harness.text = "TORONTO, February 27, 2025 - " + ARTICLE_TEXT
    harness.extractions = [{**GOOD, "news_date": "2025-02-27"}]
    harness.run(article())
    assert harness.store.tables["north_america"][0]["news_date"] == "2025-02-27"
    harness.store.tables["north_america"].clear()
    harness.extractions = [{**GOOD, "news_date": "2025-02-20"}]            # a date the announcement does not write
    harness.run(article())
    assert harness.store.tables["north_america"][0]["news_date"] == "2025-03-01"
    assert harness.queued() == []


def test_existing_project_is_updated_and_never_erased(harness):
    harness.store.tables["north_america"].append({"project_name": "Acme", "cash_payments_value": 5, "owner_name": "OldCo"})
    harness.extractions = [{**GOOD, "owner_name": None}]
    final = harness.run(article(classification={"status": "existing", "table_name": "north_america"}))
    row = harness.store.tables["north_america"][0]
    assert final["outcome"] == "updated"
    assert row["cash_payments_value"] == 100000      # corrected
    assert row["owner_name"] == "OldCo"              # a null from the model does not erase a stored value


def test_unsupported_value_gets_one_retry_with_feedback(harness):
    harness.extractions = [{**GOOD, "shares_amount": 9999999}, GOOD]
    final = harness.run(article())
    assert final["outcome"] == "inserted" and final["attempts"] == 2
    assert harness.calls[0] is None and "shares_amount" in harness.calls[1]
    assert harness.queued() == []


def test_still_unsupported_after_retry_is_withheld_and_queued(harness):
    harness.extractions = [{**GOOD, "shares_amount": 9999999}]
    final = harness.run(article())
    assert final["outcome"] == "inserted_partial" and final["attempts"] == 2
    row = harness.store.tables["north_america"][0]
    assert row["shares_amount"] is None and row["cash_payments_value"] == 100000
    queued = harness.queued()
    assert len(queued) == 1 and queued[0]["withheld_values"] == {"shares_amount": 9999999}


def test_malformed_record_is_queued_and_not_written(harness):
    harness.extractions = [{**GOOD, "cash_payments_currency": "dollars"}]
    final = harness.run(article())
    assert final["outcome"] == "review_only"
    assert harness.store.writes == [] and len(harness.queued()) == 1


def test_unclassified_article_goes_to_the_catch_all_table(harness):
    final = harness.run(article(classification={"status": "unclassified", "table_name": None}))
    assert final["outcome"] == "inserted" and len(harness.store.tables["other"]) == 1


@pytest.mark.parametrize("art, expected", [
    (article(project_name=None), "skipped_no_project"),
    (article(link=None), "skipped_no_link"),
    (article(classification={"status": "new", "table_name": None}), "skipped_no_table"),
])
def test_articles_that_cannot_be_processed_stop_before_any_model_call(harness, art, expected):
    assert harness.run(art)["outcome"] == expected
    assert harness.calls == [] and harness.store.writes == []


def test_unreadable_page_and_empty_extraction(harness):
    harness.text = None
    assert harness.run(article())["outcome"] == "skipped_no_content"
    harness.text, harness.extractions = ARTICLE_TEXT, [{}]
    assert harness.run(article())["outcome"] == "no_data"
    assert harness.store.writes == []


def test_one_trace_per_article_with_the_steps_as_spans(harness):
    exporter = InMemorySpanExporter()
    telemetry.setup(extra_processor=SimpleSpanProcessor(exporter))
    harness.extractions = [{**GOOD, "shares_amount": 9999999}, GOOD]
    harness.run(article())
    spans = exporter.get_finished_spans()
    names = [s.name for s in spans]
    assert names.count("extract") == 2 and names.count("validate") == 2 and "store" in names
    root = next(s for s in spans if s.name == "process_article")
    assert root.attributes["pipeline.outcome"] == "inserted" and root.attributes["pipeline.extract.attempts"] == 2
    assert {s.context.trace_id for s in spans} == {root.context.trace_id}
    first_check = next(s for s in spans if s.name == "validate")
    assert first_check.attributes["pipeline.validate.not_found"] == "shares_amount"


def test_committed_diagram_matches_the_compiled_graph():
    from tools.draw_graph import OUTPUT, render

    assert OUTPUT.read_text() == render(), "run `python -m tools.draw_graph` and commit docs/graph.md"
