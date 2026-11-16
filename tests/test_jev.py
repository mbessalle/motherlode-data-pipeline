"""Jev client and the extraction check: requests, cache, failures and the passage Jev reads. Offline."""
from types import SimpleNamespace

import pytest

from evals import jev, jev_verify

TEXT = ("Acme Gold Ltd announces the acquisition of the Foggy Mountain Project. Drilling at the nearby Clear Lake "
        "project returned 12 m at 3 g/t gold. " * 6 + "Under the agreement Acme will pay A$100,000 on signing and "
        "A$150,000 after six months, and will issue 2,000,000 shares to the vendor. Separately, Acme raised "
        "A$2,000,000 in a placement. " + "The Company remains focused on exploration in Western Australia. " * 20)


def test_a_request_is_sent_once_and_then_read_from_the_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    sent = []

    def post(url, headers, json, timeout):
        sent.append(json)
        assert url == jev.URL and headers["Authorization"] == "Bearer key"
        return SimpleNamespace(status_code=200, text="", json=lambda: {
            "model": json["model"], "answers": {"q": {"type": "noul", "noul": 0.9}}, "usage": {"input_tokens": 50, "cost": 2e-6}})

    question = {"q": jev.noul("Is it?", yes="when it is", no="when it is not")}
    assert jev.decide({"x": 1}, question, post=post, cache=tmp_path)["q"]["noul"] == 0.9
    assert jev.decide({"x": 1}, question, post=post, cache=tmp_path)["q"]["noul"] == 0.9
    assert len(sent) == 1 and sent[0]["questions"]["q"]["criteria"] == {"true": "when it is", "false": "when it is not"}


def test_a_failed_call_raises_instead_of_reading_as_an_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")

    def post(url, headers, json, timeout):
        return SimpleNamespace(status_code=402, text="insufficient credits", json=lambda: {})

    with pytest.raises(RuntimeError, match="402"):
        jev.decide({"x": 1}, {"q": jev.noul("Is it?")}, post=post, cache=tmp_path)
    assert not list(tmp_path.iterdir())          # nothing cached, so a retry asks again


def test_the_passage_is_the_text_around_the_value_cut_at_sentence_ends():
    text = " ".join(TEXT.split())
    found = jev_verify.passage(text, "shares_amount", 2000000, None, "FOUND")
    assert "2,000,000 shares to the vendor" in found and len(found) < 2 * jev_verify.SIDE_CHARS + 300
    assert found.startswith(("Under", "Acme", "Drilling")) and found.endswith(".")


def test_a_derived_value_shows_every_amount_its_rule_used():
    text = " ".join(TEXT.split())
    derived = jev_verify.passage(text, "cash_payments_value", 250000, "sum of 100,000 + 150,000", "DERIVED")
    assert "A$100,000 on signing" in derived and "A$150,000 after six months" in derived
    assert jev_verify.passage(text, "cash_payments_value", 999999, None, "FOUND") is None


def test_every_checked_field_has_criteria_and_a_role():
    assert set(jev_verify.FIELDS) == set(jev_verify.FIELD_ROLE)
    assert set(jev_verify.FIELD_ROLE.values()) <= set(jev_verify.ROLES)
