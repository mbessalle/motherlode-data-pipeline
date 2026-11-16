"""The end-to-end sample runner: production run() on a local PDF, compared with the analyst's row. Offline."""
import argparse
import csv
import json
from types import SimpleNamespace

import pytest

from evals import end_to_end
from pipeline import telemetry
from tests.test_pdf import digital_pdf

TEXT = ("PERTH, 4 March 2025. Acme Gold Ltd is pleased to announce it has agreed to acquire a 70% interest in the "
        "Foggy Mountain Project in Western Australia for A$250,000 cash and 2,000,000 shares. The vendor retains a "
        "2% NSR royalty. The project covers 4,500 hectares. " + "Acme Gold will fund exploration at Foggy Mountain. " * 25)
ROW = {"id": 900, "project_name": "Foggy Mountain", "news_title": "Acme Gold to Acquire Foggy Mountain Project",
       "owner_name": "Acme Gold Ltd", "news_date": "2025-03-04", "news_link": "https://listing.test/900",
       "interest_acquired_percent": 70, "cash_payments_value": 250000, "cash_payments_currency": "AUD",
       "shares_amount": 2000000, "nsr_percent": 2, "project_area": 45, "project_area_unit": "km2"}
REPLIES = {
    "triage": {"is_project_deal": True, "project_name": "Foggy Mountain", "reason": "acquisition of a project interest"},
    "classify": {"country": "Australia", "continent": "Oceania"},
    "extract": {"reasoning": {}, "database_updates": {"interest_acquired_percent": 70, "cash_payments_value": 250000,
                                                      "cash_payments_currency": "AUD", "shares_amount": 2000000,
                                                      "nsr_percent": 3, "project_area": 4500, "project_area_unit": "ha"}},
}


class FakeOpenAI:
    def __init__(self):
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, model, messages, response_format):
        self.calls += 1
        system = messages[0]["content"]
        kind = ("triage" if system == end_to_end.TRIAGE_SYSTEM_PROMPT else
                "classify" if system == end_to_end.CLASSIFIER_SYSTEM_PROMPT else "extract")
        return SimpleNamespace(model=model, choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(REPLIES[kind])))],
                               usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=100))


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry, "_provider", None)       # a fresh tracer provider that reads this test's settings
    for name in ("PIPELINE_TRACE_FILE", "PIPELINE_TRACE_CONTENT", "OTEL_EXPORTER_OTLP_ENDPOINT", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "pdfs" / "foggy").mkdir(parents=True)
    (tmp_path / "pdfs" / "foggy" / "foggy_1.pdf").write_bytes(digital_pdf(TEXT))
    case = {"id": "foggy_1-900", "row_id": 900, "project": "foggy", "file": "foggy_1.pdf",
            "written_in_document": ["interest_acquired_percent", "cash_payments_value", "shares_amount", "nsr_percent"],
            "derived_in_document": {"project_area": "km2 to ha"}}
    (tmp_path / "cases.jsonl").write_text(json.dumps(case) + "\n")
    with open(tmp_path / "rows.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["source", "rows"])
        writer.writerow(["oceania", json.dumps([ROW])])
    return argparse.Namespace(sample=30, seed=1, cases=tmp_path / "cases.jsonl", rows=tmp_path / "rows.csv",
                              pdfs=tmp_path / "pdfs", out=tmp_path / "out", replies=tmp_path / "replies",
                              model="gpt-6.1-terra", classify_model="gpt-6-luna",
                              no_headline_filter=False, no_date_check=False, openrouter=False)


def test_a_sampled_deal_runs_end_to_end_and_is_compared_with_the_analyst(inputs):
    model = FakeOpenAI()
    out = end_to_end.run_sample(inputs, inner=model)

    report = json.loads((out / "run_report.json").read_text())
    assert [e["outcome"] for e in report] == ["inserted_partial"]   # the 3% NSR is not in the text and is withheld
    assert report[0]["search_tried"][0]["verdict"].startswith("3 of 3")
    verdicts = {c["field"]: c for c in map(json.loads, (out / "comparison.jsonl").read_text().splitlines())}
    assert verdicts["cash_payments_value"]["verdict"] == "agree"
    assert verdicts["project_area"]["verdict"] == "agree"            # 45 km2 and 4,500 ha are the same area
    assert verdicts["nsr_percent"] == {**verdicts["nsr_percent"], "verdict": "analyst_only", "withheld": True}

    spans = [json.loads(line) for line in (out / "spans.jsonl").read_text().splitlines()]
    chats = [s for s in spans if s["name"].startswith("chat ")]
    assert len(chats) == 4                                           # triage, region, two extraction attempts
    assert all("gen_ai.output.messages" in s["attributes"] for s in chats)
    assert {s["trace_id"] for s in spans} == {spans[-1]["trace_id"]}   # one run, one trace
    assert "spend of this run: 0.0" in (out / "summary.md").read_text()


def test_a_second_run_of_the_same_sample_reads_every_reply_from_the_cache(inputs, monkeypatch):
    model = FakeOpenAI()
    end_to_end.run_sample(inputs, inner=model)
    first = model.calls
    monkeypatch.setattr(telemetry, "_provider", None)
    inputs.out = inputs.out.parent / "second"
    end_to_end.run_sample(inputs, inner=model)
    assert first == 4 and model.calls == first
    assert "cached: 4, paid: 0" in (inputs.out / "summary.md").read_text()


def test_areas_are_compared_in_hectares():
    assert end_to_end.hectares(45, "km²") == 4500 and end_to_end.hectares("4,500", "Ha") == 4500
    assert end_to_end.hectares(10, "furlongs") is None and end_to_end.hectares(None, "ha") is None
