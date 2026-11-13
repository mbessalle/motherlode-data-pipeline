"""The run as a whole, offline: listing pagination, state handling, classification, LLM tracing attributes."""
import json
from datetime import UTC, datetime
from types import SimpleNamespace

from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from pipeline import config, llm, run, sources, telemetry
from pipeline.store import MemoryStore


class FakeClient:
    """Stands in for the OpenAI client: returns queued JSON replies and records what it was asked."""

    def __init__(self, replies):
        self.replies, self.requests = list(replies), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(model=kwargs["model"], usage=SimpleNamespace(prompt_tokens=1200, completion_tokens=300),
                               choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(reply)))])


def test_new_since_stops_at_the_last_processed_article_and_returns_oldest_first():
    listing = [{"id": 5}, {"id": 4}, {"id": 3}, {"id": 2}]
    assert [a["id"] for a in sources.new_since(listing, "3")] == [4, 5]
    assert [a["id"] for a in sources.new_since(listing, "")] == [2, 3, 4, 5]
    assert sources.new_since(listing, "5") == []


def test_listing_pagination_moves_the_window_and_drops_duplicates():
    pages = [[{"id": 3, "date": "2025-03-01T12:00:00Z"}, {"id": 2, "date": "2025-03-01T09:00:00Z"}],
             [{"id": 2, "date": "2025-03-01T09:00:00Z"}, {"id": 1, "date": "2025-03-01T08:00:00Z"}], []]
    sent = []

    def post(url, headers, json, timeout):
        sent.append(json["daterange"]["endDate"])
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"data": pages[len(sent) - 1]})

    now = datetime(2025, 3, 1, 13, 0, tzinfo=UTC)
    articles = sources.fetch_todays_articles(post=post, now=now, pause=0)
    assert [a["id"] for a in articles] == [3, 2, 1]
    assert sent == ["2025-03-01T13:00:00.000Z", "2025-03-01T08:59:59.000Z", "2025-03-01T07:59:59.000Z"]


def test_llm_calls_carry_genai_attributes_and_survive_failures():
    exporter = InMemorySpanExporter()
    telemetry.setup(extra_processor=SimpleSpanProcessor(exporter))
    client = FakeClient([{"country": "Canada", "continent": "North America"}, RuntimeError("rate limited")])
    assert llm.classify_location(client, "model-a", "tags: 'Canada'") == {"country": "Canada", "continent": "North America"}
    assert llm.extract_deal(client, "model-b", "article", {"project_name": "Acme"}) == {}
    ok, failed = exporter.get_finished_spans()
    assert ok.attributes["gen_ai.request.model"] == "model-a" and ok.attributes["gen_ai.operation.name"] == "chat"
    assert ok.attributes["gen_ai.usage.input_tokens"] == 1200 and ok.attributes["gen_ai.usage.output_tokens"] == 300
    assert failed.attributes["error.type"] == "RuntimeError"
    assert client.requests[1]["response_format"] == {"type": "json_object"}


def test_retry_prompt_names_the_unsupported_values():
    prompt = llm.build_extraction_prompt("text", {"project_name": "Acme", "owner_name": None}, "- shares_amount: 5e+06")
    assert "Correction required" in prompt and "- shares_amount: 5e+06" in prompt and "owner_name" not in prompt
    assert "Correction required" not in llm.build_extraction_prompt("text", {"project_name": "Acme"})


def test_full_run_offline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sources.write_state(run.STATE_FILE, "10")
    listing = [
        {"id": 12, "project_name": "Acme", "link": "https://example.test/12", "title": "NewCo buys Acme", "date": "2025-03-01T10:00:00Z",
         "tags": "Canada", "description": ""},
        {"id": 11, "project_name": None, "link": "https://example.test/11", "title": "Market wrap", "date": "2025-03-01T09:00:00Z"},
        {"id": 10, "project_name": "Old", "link": "https://example.test/10", "date": "2025-03-01T08:00:00Z"},
    ]
    client = FakeClient([{"country": "Canada", "continent": "North America"},
                         {"reasoning_and_acknowledgement": {}, "database_updates": {"cash_payments_value": 100000, "nsr_percent": 2}}])
    store = MemoryStore({"north_america": []})
    settings = config.Settings(None, None, None, None, None, "k", "extract-model", "classify-model", 2, str(tmp_path / "review.jsonl"))
    summary = run.run(settings=settings, store=store, client=client, fetch=lambda: listing,
                      scrape=lambda url: "NewCo will pay $100,000 in cash. The vendor keeps a 2% NSR.")
    assert summary == {"articles": 2, "outcomes": {"skipped_no_project": 1, "inserted": 1}}
    assert store.tables["north_america"][0]["project_name"] == "Acme"
    assert [r["model"] for r in client.requests] == ["classify-model", "extract-model"]
    assert sources.read_state(run.STATE_FILE) == "12"
