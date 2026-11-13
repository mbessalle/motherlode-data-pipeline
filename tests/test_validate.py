"""The validator decides what reaches the database, so each rule is pinned by a case that would break it."""
import pytest

from pipeline.validate import DERIVED, EMPTY, FOUND, NOT_FOUND, find_number, validate, written_amounts

OPTION = (
    "Vancouver, May 31, 2023. The Company may exercise the Option by paying $250,000 in cash on closing and "
    "$250,000 on the first anniversary, issuing 2,000,000 common shares at a deemed price of $0.25 per share, "
    "and incurring $1,000,000 of expenditures on the five Option Projects. The vendor retains a 2% NSR royalty. "
    "The property covers 4,857 hectares and the Company will earn a 100% interest."
)


def status(extraction, text=OPTION):
    return {name: check.status for name, check in validate(extraction, text).fields.items()}


def test_values_written_in_the_article_are_found():
    s = status({"shares_amount": 2000000, "share_price": 0.25, "nsr_percent": 2, "interest_acquired_percent": 100,
                "project_area": 4857})
    assert {s[k] for k in ("shares_amount", "share_price", "nsr_percent", "interest_acquired_percent", "project_area")} == {FOUND}


def test_evidence_snippet_is_kept():
    check = validate({"shares_amount": 2000000}, OPTION).fields["shares_amount"]
    assert "2,000,000 common shares" in check.evidence


def test_invented_number_is_not_found_and_blocks():
    report = validate({"shares_amount": 3500000, "nsr_percent": 2}, OPTION)
    assert report.fields["shares_amount"].status == NOT_FOUND
    assert report.not_found == ["shares_amount"] and not report.passed
    assert "shares_amount: 3.5e+06" in report.feedback()


def test_empty_fields_do_not_block():
    report = validate({"shares_amount": None, "cash_payments_value": "", "nsr_percent": 0}, OPTION)
    assert set(status({"shares_amount": None}).values()) == {EMPTY}
    assert report.passed and report.checked == 0


def test_cash_as_sum_of_tranches_is_derived():
    check = validate({"cash_payments_value": 500000}, OPTION).fields["cash_payments_value"]
    assert check.status == DERIVED and check.evidence == "sum of 250,000 + 250,000"


def test_an_amount_is_reused_only_if_written_twice():
    once = "The Company will pay $250,000 in cash and issue shares."
    assert status({"cash_payments_value": 500000}, once)["cash_payments_value"] == NOT_FOUND


def test_commitment_shared_across_projects_is_derived():
    check = validate({"exp_comit_value": 200000}, OPTION).fields["exp_comit_value"]
    assert check.status == DERIVED and check.evidence == "1,000,000 split across 5 projects"


def test_shares_value_is_shares_times_price_only_with_supported_inputs():
    good = status({"shares_amount": 2000000, "share_price": 0.25, "shares_value": 500000})
    assert good["shares_value"] in (DERIVED, FOUND)
    # 3,000,000 shares are not in the article, so their product is not accepted either
    bad = status({"shares_amount": 3000000, "share_price": 0.25, "shares_value": 750000})
    assert bad["shares_value"] == NOT_FOUND


def test_aggregate_is_sum_of_supported_parts():
    extraction = {"cash_payments_value": 500000, "exp_comit_value": 200000, "shares_amount": 2000000, "share_price": 0.25,
                  "shares_value": 500000, "aggregate_deal_value": 1200000}
    report = validate(extraction, OPTION)
    assert report.fields["aggregate_deal_value"].status == DERIVED and report.passed


def test_aggregate_with_unsupported_part_is_not_accepted():
    extraction = {"cash_payments_value": 777000, "aggregate_deal_value": 777000}
    assert status(extraction)["aggregate_deal_value"] == NOT_FOUND


def test_drilling_metres_convert_at_250_usd():
    text = "The Company has committed to 4,000 metres of drilling in the first year."
    check = validate({"exp_comit_value": 1000000}, text).fields["exp_comit_value"]
    assert check.status == DERIVED and "4,000 metres" in check.evidence


@pytest.mark.parametrize("text, value, percent, expected", [
    ("a payment of $1.5 million on closing", 1500000, False, True),
    ("a payment of US$250K on signing", 250000, False, True),
    ("a two percent net smelter royalty", 2, True, True),
    ("a 2.0% NSR", 2, True, True),
    ("issued 12,000,000 shares", 2000000, False, False),   # must not match inside a longer number
    ("priced at $0.255 per share", 0.25, False, False),
    ("in fiscal 2023 the company", 2023, False, True),      # written, but see written_amounts for sums
    ("holds a 20% interest", 2, True, False),
    ("can acquire a seventy-five percent interest", 75, True, True),
    ("up to a one-hundred percent interest", 100, True, True),
    ("a fifty one per cent stake", 51, True, True),
    ("a seventy-five percent interest", 5, True, False),      # "five percent" inside "seventy-five percent"
])
def test_number_matching(text, value, percent, expected):
    assert bool(find_number(text, value, percent)) is expected


def test_years_and_bare_numbers_are_not_amounts():
    text = "In 2023 and 2024 the Company spent $1,950 and 2,024 dollars, then 3 million more over 2025."
    assert written_amounts(text) == [1950.0, 2024.0, 3000000.0]


def test_sums_never_mix_dollars_and_shares():
    text = "The Company will pay $1,000,000 and issue 2,000,000 common shares and 500,000 warrants."
    assert written_amounts(text, exclude="shares") == [1000000.0]
    assert written_amounts(text, exclude="money") == [500000.0, 2000000.0]
    # 3,000,000 shares would only be reachable by adding dollars to shares
    assert status({"shares_amount": 3000000}, text)["shares_amount"] == NOT_FOUND
    assert status({"shares_amount": 2500000}, text)["shares_amount"] == DERIVED


def test_schema_issues_block_even_when_values_are_grounded():
    report = validate({"nsr_percent": 2, "cash_payments_currency": "dollars", "news_date": "31/05/2023",
                       "shares_amount": "two million", "interest_acquired_percent": 150}, OPTION)
    assert not report.passed
    joined = " | ".join(report.schema_issues)
    for expected in ("cash_payments_currency", "news_date", "shares_amount", "interest_acquired_percent out of range"):
        assert expected in joined


def test_shares_value_from_a_price_the_article_states():
    text = "The Company issued 14,195,506 common shares at a deemed price of $0.1278 per share."
    report = validate({"shares_amount": 14195506, "shares_value": 1814185.67}, text)
    assert report.fields["shares_value"].status == DERIVED and "0.1278" in report.fields["shares_value"].evidence
    # a value that needs a price nobody wrote stays unsupported
    assert validate({"shares_amount": 14195506, "shares_value": 5000000}, text).fields["shares_value"].status == NOT_FOUND


def test_a_share_count_is_not_evidence_for_a_cash_payment():
    text = ("Consideration is 2,000,000 common shares issuable in four tranches of 500,000 shares each, "
            "plus a cash payment of $150,000 on closing.")
    report = validate({"cash_payments_value": 500000, "exp_comit_value": 2000000, "shares_amount": 2000000}, text)
    assert report.fields["cash_payments_value"].status == NOT_FOUND     # 500,000 is only written as shares
    assert report.fields["exp_comit_value"].status == NOT_FOUND         # 2,000,000 is only written as shares
    assert report.fields["shares_amount"].status == FOUND
    assert validate({"cash_payments_value": 150000}, text).fields["cash_payments_value"].status == FOUND
    assert validate({"shares_amount": 150000}, text).fields["shares_amount"].status == NOT_FOUND   # $150,000 is money
