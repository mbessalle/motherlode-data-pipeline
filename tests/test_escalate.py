"""The escalation tier: only flagged deals go up, and code checks the frontier model's quotes. Offline."""
import json
from types import SimpleNamespace

import pytest

from evals import escalate

DOC = ("Fenix Resources Limited has agreed binding terms with Spartan Resources Limited to acquire E51/1681: "
       "Cash consideration of A$250,000 and a milestone payment of A$1,000,000 upon the sale of 1,000,000 tonnes of iron ore.")


def test_it_refuses_to_bill_an_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    with pytest.raises(RuntimeError, match="subscription"):
        escalate.claude_code("rules", "prompt", {}, run=lambda *a, **k: None)


def test_a_quote_must_be_in_the_document():
    assert escalate.quote_found("Cash consideration of A$250,000 and a milestone payment of A$1,000,000", DOC)
    assert escalate.quote_found("agreed binding terms ... to acquire E51/1681", DOC)
    assert not escalate.quote_found("Cash consideration of A$500,000", DOC)
    assert not escalate.quote_found("", DOC)


def test_a_value_must_be_written_in_its_quote_or_follow_by_a_rule():
    quote = "Cash consideration of A$250,000 and a milestone payment of A$1,000,000"
    assert escalate.value_supported(1_250_000, quote, "cash_payments_value")          # sum
    assert escalate.value_supported(250_000, quote, "cash_payments_value")            # written
    assert escalate.value_supported(83_750_000, "Equity consideration of $167.5 million", "shares_value")   # split by 2
    assert not escalate.value_supported(3_000_000, quote, "cash_payments_value")


def test_only_deals_with_an_unsettled_cell_or_buyer_go_up(tmp_path):
    cells = [{"row_id": 1, "project": "A", "field": "cash_payments_value", "action": "accept"},
             {"row_id": 2, "project": "B", "field": "cash_payments_value", "action": "review"},
             {"row_id": 3, "project": "C", "field": "nsr_percent", "action": "correct"},
             {"row_id": 3, "project": "D", "field": "nsr_percent", "action": "accept"}]     # same deal, other project
    parties = [{"row_id": 1, "project": "A", "action": "accept"}, {"row_id": 4, "project": "E", "action": "review"},
               {"row_id": 2, "project": "B", "action": "accept"}]
    (tmp_path / "cells.jsonl").write_text("".join(json.dumps(c) + "\n" for c in cells))
    (tmp_path / "parties.jsonl").write_text("".join(json.dumps(p) + "\n" for p in parties))
    flags = escalate.flagged(tmp_path)
    assert sorted(flags) == [(2, "B"), (3, "C"), (4, "E")]
    assert flags[(2, "B")]["party"] == {"row_id": 2, "project": "B", "action": "accept"}


def test_the_answer_is_checked_and_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(escalate, "CACHE", tmp_path)
    answer = {"cells": [{"field": "cash_payments_value", "verdict": "correct", "value": 1_250_000, "unit": "AUD",
                         "quote": "Cash consideration of A$250,000 and a milestone payment of A$1,000,000", "reason": "sum"},
                        {"field": "nsr_percent", "verdict": "wrong", "value": 3, "unit": None, "quote": "a 3% royalty", "reason": "invented"}],
              "buyer": {"name": "Fenix Resources Limited", "quote": "Fenix Resources Limited has agreed binding terms", "reason": "acquires"}}
    calls = []

    def run(command, input, capture_output, text, timeout):
        calls.append((command, input))
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
            {"structured_output": answer, "usage": {"input_tokens": 900, "output_tokens": 200}, "total_cost_usd": 0.01}))

    call = lambda system, user, schema: escalate.claude_code(system, user, schema, run=run)   # noqa: E731
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    flags = {"cells": [{"field": "cash_payments_value", "value": 1_250_000, "source": "computed", "p_correct": 0.5}], "party": None}
    first, usage = escalate.ask_model("Beebynganna Hills", flags, DOC, call)
    again, usage_again = escalate.ask_model("Beebynganna Hills", flags, DOC, call)
    command = calls[0][0]
    assert len(calls) == 1 and usage_again["cached"] and usage["input_tokens"] == 900
    assert command[:4] == ["claude", "-p", "--model", "opus"] and command[command.index("--tools") + 1] == ""
    assert "--json-schema" in command and "Cash consideration" in calls[0][1]
    checked = escalate.check_answer(first, DOC)
    assert [c["quote_checked"] for c in checked["cells"]] == [True, False] and checked["buyer"]["quote_checked"]
