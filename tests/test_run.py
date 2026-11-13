"""The run as a whole, offline: discovery state, search, triage, classification, LLM tracing attributes."""
import json
from types import SimpleNamespace

from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from pipeline import config, llm, run, sources, telemetry
from pipeline.search import Source
from pipeline.store import MemoryStore

RELEASE = "Nov. 10, 2026. NewCo will pay $100,000 in cash to acquire the Acme project. The vendor keeps a 2% NSR."


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


class FakeSearcher:
    """Returns a prepared announcement per article id, or nothing."""

    def __init__(self, found: dict[int, str]):
        self.found, self.searches = found, 0

    def find_announcement(self, article):
        self.searches += 1
        text = self.found.get(article["id"])
        tried = [{"url": f"https://newswire.test/{article['id']}", "read": "html", "verdict": "ok" if text else "too short"}]
        return (Source(tried[0]["url"], text, "html") if text else None), tried


def listing(*items):
    return [{"id": i, "title": t, "date": "2026-11-11T02:00:00.000Z", "company": "newco inc",
             "listing_link": f"https://mininghub.com/company/newco-inc/insights/x-{i}"} for i, t in items]


def settings_for(tmp_path, **overrides):
    return config.Settings(None, None, None, None, None, "k", "extract-model", "classify-model", 2, str(tmp_path / "review.jsonl"),
                           run_report_path=str(tmp_path / "report.json"), serper_api_key="s", **overrides)


def test_new_since_stops_at_the_last_processed_article_and_returns_oldest_first():
    feed = [{"id": 5}, {"id": 4}, {"id": 3}, {"id": 2}]
    assert [a["id"] for a in sources.new_since(feed, "3")] == [4, 5]
    assert [a["id"] for a in sources.new_since(feed, "")] == [2, 3, 4, 5]
    assert [a["id"] for a in sources.new_since(feed, "99")] == [2, 3, 4, 5]     # the marker left the feed: all are new
    assert sources.new_since(feed, "5") == []


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


def test_triage_normalises_the_reply_and_treats_a_failed_call_as_no():
    client = FakeClient([{"is_project_deal": True, "project_name": " Acme ", "reason": "sale"},
                         {"is_project_deal": "yes", "project_name": ""}, RuntimeError("down")])
    assert llm.triage(client, "m", "t", "c", "text") == {"is_project_deal": True, "project_name": "Acme", "reason": "sale"}
    assert llm.triage(client, "m", "t", "c", "text")["is_project_deal"] is False       # only a real true counts
    assert llm.triage(client, "m", "t", "c", "text") == {"is_project_deal": False, "project_name": None, "reason": None}


def test_retry_prompt_names_the_unsupported_values():
    prompt = llm.build_extraction_prompt("text", {"project_name": "Acme", "owner_name": None}, "- shares_amount: 5e+06")
    assert "Correction required" in prompt and "- shares_amount: 5e+06" in prompt and "owner_name" not in prompt
    assert "Correction required" not in llm.build_extraction_prompt("text", {"project_name": "Acme"})


def test_full_run_offline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sources.write_state(run.STATE_FILE, "10")
    feed = listing((15, "NewCo Acquires Zeta Project"), (14, "NewCo Sells First Concentrate"), (13, "NewCo Acquires Acme Project"),
                   (12, "NewCo Options Missing Project"), (11, "NewCo Drills 10 m of Gold"), (10, "Old news"))
    client = FakeClient([
        {"is_project_deal": True, "project_name": "Acme", "reason": "purchase"},                 # 13 triage
        {"country": "Canada", "continent": "North America"},                                     # 13 region
        {"reasoning_and_acknowledgement": {}, "database_updates": {"cash_payments_value": 100000, "nsr_percent": 2}},
        {"is_project_deal": False, "project_name": None, "reason": "metal sales"},               # 14 triage
        {"is_project_deal": True, "project_name": "Zeta", "reason": "purchase"},                 # 15 triage: not in the text
    ])
    searcher = FakeSearcher({13: RELEASE, 14: "NewCo sold its first concentrate. " * 5, 15: RELEASE})
    store = MemoryStore({"north_america": []})
    summary = run.run(settings=settings_for(tmp_path), store=store, client=client, searcher=searcher, fetch=lambda: feed)

    assert summary == {"articles": 5, "candidates": 4, "outcomes": {
        "source_not_found": 1, "inserted": 1, "not_a_project_deal": 1, "project_name_not_in_source": 1}}
    assert searcher.searches == 4                                         # the drilling headline cost no search
    row = store.tables["north_america"][0]
    assert row["project_name"] == "Acme" and row["news_link"] == "https://newswire.test/13" and row["cash_payments_value"] == 100000
    assert [r["model"] for r in client.requests] == ["classify-model", "classify-model", "extract-model", "classify-model", "classify-model"]
    assert sources.read_state(run.STATE_FILE) == "15"
    report = json.loads((tmp_path / "report.json").read_text())
    assert [(r["id"], r["outcome"]) for r in report] == [(12, "source_not_found"), (13, "inserted"), (14, "not_a_project_deal"),
                                                         (15, "project_name_not_in_source")]
    assert report[1]["source_url"] == "https://newswire.test/13" and report[1]["validation"]["passed"]
    assert report[0]["search_tried"][0]["verdict"] == "too short"


def test_article_limit_counts_candidates_and_leaves_the_rest_for_the_next_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    feed = listing((6, "A Acquires F"), (5, "Drill results"), (4, "A Acquires D"), (3, "A Acquires C"), (2, "Quarterly report"),
                   (1, "A Acquires A"))
    kwargs = dict(settings=settings_for(tmp_path, max_articles=2), store=MemoryStore(), client=FakeClient([]),
                  searcher=FakeSearcher({}), fetch=lambda: feed)
    first = run.run(**kwargs)
    assert (first["articles"], first["candidates"], sources.read_state(run.STATE_FILE)) == (3, 2, "3")     # 1, (2), 3
    second = run.run(**kwargs)
    assert (second["articles"], second["candidates"], sources.read_state(run.STATE_FILE)) == (3, 2, "6")   # 4, (5), 6
    assert run.run(**kwargs) == {"articles": 0, "candidates": 0, "outcomes": {}}
