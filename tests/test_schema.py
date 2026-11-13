"""The Pydantic contract between the extraction prompt and the database."""
import pytest
from pydantic import ValidationError

from pipeline import schema
from pipeline.schema import DealExtraction, TriageVerdict


def test_valid_extraction_is_typed_and_unknown_keys_are_dropped():
    raw = {"project_name": " Acme ", "cash_payments_value": "2,000,000", "share_price": "$0.25", "nsr_percent": 2,
           "cash_payments_currency": "CAD", "news_date": "2026-11-10", "owner_name": "", "shares_amount": None,
           "reasoning": "not a column"}
    assert schema.issues(raw) == []
    assert schema.normalise(raw) == {"project_name": "Acme", "cash_payments_value": 2000000.0, "share_price": 0.25,
                                     "nsr_percent": 2.0, "cash_payments_currency": "CAD", "news_date": "2026-11-10"}


@pytest.mark.parametrize("field, value", [
    ("cash_payments_value", "two million"), ("shares_amount", True), ("nsr_percent", 150), ("interest_acquired_percent", -1),
    ("latitude", 91), ("longitude", -181), ("cash_payments_currency", "dollars"), ("aggregate_deal_currency", "usd"),
    ("news_date", "31/05/2023"),
])
def test_each_rule_reports_its_field(field, value):
    found = schema.issues({field: value})
    assert len(found) == 1 and found[0].startswith(field + ":")
    with pytest.raises(ValidationError):
        DealExtraction.model_validate({field: value})


def test_every_field_the_prompt_lists_is_in_the_schema():
    from pipeline.prompts import EXTRACTOR_SYSTEM_PROMPT

    listed = [line.split(":")[0][2:] for line in EXTRACTOR_SYSTEM_PROMPT.splitlines() if line.startswith("- ") and ": (" in line]
    assert len(listed) == 36 and set(listed) == set(DealExtraction.model_fields)


def test_triage_verdict_accepts_only_a_real_boolean():
    assert TriageVerdict.model_validate({"is_project_deal": True, "project_name": " Acme "}).project_name == "Acme"
    with pytest.raises(ValidationError):
        TriageVerdict.model_validate({"is_project_deal": "yes"})
    assert TriageVerdict().model_dump() == {"is_project_deal": False, "project_name": None, "reason": None}


def test_explicit_nulls_and_blanks_are_valid_for_every_field():
    """Models return null for most fields. Found by the eval: a range on a nullable field once rejected null."""
    for empty in (None, "", "  "):
        assert schema.issues({name: empty for name in DealExtraction.model_fields}) == [], repr(empty)
    assert schema.normalise({name: None for name in DealExtraction.model_fields}) == {}
