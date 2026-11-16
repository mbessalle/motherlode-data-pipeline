"""The cell check: code finds every place a value is written, Jev judges and picks. Offline (Jev is faked)."""
import datetime as dt
from types import SimpleNamespace

import pytest

from evals import cell_check, jev, market

TEXT = ("Acme Gold Ltd (ASX:ACM) will acquire a 70% interest in the Foggy Mountain Project from Vendor Metals Pty Ltd. "
        "Drilling returned 1m @ 6.15 g/t gold and 4m @ 3.09 g/t gold. "
        "Acme will pay A$100,000 on signing and A$150,000 after six months, and will issue 2,000,000 shares at a deemed "
        "price of 5 cents per share. The project covers 45 km2. Acme separately raised A$1.5m in a placement. "
        "Vendor Metals will retain a 2% NSR royalty.")


def test_a_drill_intercept_is_not_a_million_but_a_currency_amount_is():
    assert cell_check.number_spans(TEXT, 1_000_000, kind="money") == []
    assert cell_check.number_spans("Acme will pay $1m on signing.", 1_000_000, kind="money")
    assert len(cell_check.number_spans("A$150,000 now and A$150,000 later", 150_000, kind="money")) == 2


def test_a_share_count_is_not_cash_and_cash_is_not_a_share_count():
    assert cell_check.number_spans(TEXT, 2_000_000, kind="money") == []
    assert cell_check.number_spans(TEXT, 2_000_000, kind="shares")


def test_candidates_are_parsed_by_the_fields_kind():
    money = [c["value"] for c in cell_check.candidates(TEXT, "cash_payments_value")]
    assert money[:3] == [100_000, 150_000, 1_500_000]
    assert [c["value"] for c in cell_check.candidates(TEXT, "shares_amount")] == [2_000_000]
    assert [c["value"] for c in cell_check.candidates(TEXT, "share_price")] == [0.05]
    assert [c["value"] for c in cell_check.candidates(TEXT, "project_area")] == [4500]          # km2 to hectares
    assert {c["value"] for c in cell_check.candidates(TEXT, "interest_acquired_percent")} == {70, 2}


def test_the_field_passage_keeps_the_sentences_about_that_field():
    text = " ".join(TEXT.split())
    assert "NSR royalty" in cell_check.field_passage(text, "nsr_percent")
    assert "6.15 g/t" not in cell_check.field_passage(text, "nsr_percent")


@pytest.mark.parametrize("p, picked, expected", [
    (0.9, None, "accept"),
    (0.6, None, "review"),
    (0.2, {"proposal": 250_000, "p_proposal": 0.9, "proposal_is_current": False}, "correct"),
    (0.2, {"proposal": 250_000, "p_proposal": 0.6, "proposal_is_current": False}, "review"),
    (0.6, {"proposal": 250_000, "p_proposal": 0.9, "proposal_is_current": False}, "review"),   # unsure, not wrong
    (None, {"proposal": 100, "p_proposal": 0.95, "proposal_is_current": True}, "review"),
])
def test_actions_follow_the_thresholds(p, picked, expected):
    assert cell_check.action(p, picked) == expected


def fake_jev(answers):
    """A stand-in for jev.decide: answers by question name, recording what was asked."""
    asked = []

    def decide(state, questions, **kwargs):
        asked.append((state, questions))
        return {name: answers[name](state, questions[name]) for name in questions}
    return decide, asked


def test_a_stated_value_is_judged_at_every_place_and_the_best_counts(monkeypatch):
    text = " ".join(TEXT.split())
    decide, asked = fake_jev({
        "correct": lambda s, q: {"type": "noul", "noul": 0.9 if "signing" in s["document_passage"] else 0.1},
        "role": lambda s, q: {"type": "choice", "choice": "cash_payment", "confidence": 0.8},
    })
    monkeypatch.setattr(jev, "decide", decide)
    cell = cell_check.check_cell(text, "Foggy Mountain", "cash_payments_value", 100_000, "AUD", {"status": "FOUND"}, None, None)
    assert cell["source"] == "stated" and cell["p_correct"] == 0.9 and cell["action"] == "accept"


def test_a_wrong_value_gets_a_proposal_from_the_candidates(monkeypatch):
    text = " ".join(TEXT.split())
    decide, asked = fake_jev({
        "correct": lambda s, q: {"type": "noul", "noul": 0.05},
        "role": lambda s, q: {"type": "choice", "choice": "financing"},
        "pick": lambda s, q: {"type": "choice", "choice": next(k for k, v in q["criteria"].items() if "100,000" in v),
                              "probabilities": {next(k for k, v in q["criteria"].items() if "100,000" in v): 0.92}},
    })
    monkeypatch.setattr(jev, "decide", decide)
    cell = cell_check.check_cell(text, "Foggy Mountain", "cash_payments_value", 1_500_000, "AUD", {"status": "FOUND"}, None, None)
    # cash can be a sum of several written amounts and Jev picks one, so the pick is shown, not applied
    assert cell["action"] == "review" and cell["proposal"] == 100_000 and cell["p_proposal"] == 0.92


def test_a_single_number_field_is_corrected(monkeypatch):
    text = "Acme can earn a 51% interest in the Briggs project by spending $2m; it holds 30% of Briggs Pty Ltd."
    decide, asked = fake_jev({
        "correct": lambda s, q: {"type": "noul", "noul": 0.04},
        "role": lambda s, q: {"type": "choice", "choice": "other"},
        "pick": lambda s, q: {"type": "choice", "choice": "o1", "probabilities": {"o1": 0.82}},
    })
    monkeypatch.setattr(jev, "decide", decide)
    cell = cell_check.check_cell(text, "Briggs", "interest_acquired_percent", 30, None, {"status": "FOUND"}, None, None)
    assert cell["action"] == "correct" and cell["proposal"] == 51


def test_the_number_of_projects_is_read_from_a_group_name():
    assert cell_check.projects_named("Telfer and Havieron") == 2
    assert cell_check.projects_named("Bonnie Vale & Breakaway Dam, Lake Johnston") == 3
    assert cell_check.projects_named("Foggy Mountain") == 1 and cell_check.projects_named("") == 1


def test_a_share_price_not_in_the_document_is_compared_with_the_market(monkeypatch):
    monkeypatch.setattr(market, "close_on", lambda ticker, day: {"symbol": "ACM.AX", "date": day, "close": 0.052, "currency": "AUD"})
    cell = cell_check.check_cell("No price here.", "Foggy", "share_price", 0.05, None, {"status": "NOT_FOUND"}, "ASX:ACM", "2025-03-04")
    assert cell["source"] == "market" and cell["action"] == "accept"
    cell = cell_check.check_cell("No price here.", "Foggy", "share_price", 0.08, None, {"status": "NOT_FOUND"}, "ASX:ACM", "2025-03-04")
    assert cell["action"] == "correct" and cell["proposal"] == 0.052


def test_every_company_in_the_passage_gets_a_buyer_probability(monkeypatch):
    text = " ".join(TEXT.split())
    decide, asked = fake_jev({
        "buyer": lambda s, q: {"type": "choice", "choice": "c2", "probabilities": {"c1": 0.04, "c2": 0.95, "none": 0.01}},
        "named_company_is_buyer": lambda s, q: {"type": "noul", "noul": 0.97 if s["named_company"].startswith("Acme") else 0.03},
        "role": lambda s, q: {"type": "choice", "choice": "seller"},
        "project_is_object": lambda s, q: {"type": "noul", "noul": 0.9},
    })
    monkeypatch.setattr(jev, "decide", decide)
    out = cell_check.check_parties(text, {"owner_name": "Vendor Metals Pty Ltd"}, "Foggy Mountain", "Acme Gold Ltd")
    assert out["companies"][:2] == ["Vendor Metals Pty Ltd", "Acme Gold Ltd"]
    assert out["p_buyer_by_company"] == {"Vendor Metals Pty Ltd": 0.04, "Acme Gold Ltd": 0.95, "none": 0.01}
    assert out["p_owner_is_buyer"] == 0.03 and out["owner_role"] == "seller"
    assert out["action"] == "correct" and out["proposal"] == "Acme Gold Ltd"


def test_a_buyer_correction_needs_the_proposed_company_to_be_the_buyer_on_its_own(monkeypatch):
    text = " ".join(TEXT.split())
    decide, asked = fake_jev({
        "buyer": lambda s, q: {"type": "choice", "choice": "c2", "probabilities": {"c1": 0.04, "c2": 0.95, "none": 0.01}},
        "named_company_is_buyer": lambda s, q: {"type": "noul", "noul": 0.03},      # nobody is the buyer on its own
        "role": lambda s, q: {"type": "choice", "choice": "seller"},
        "project_is_object": lambda s, q: {"type": "noul", "noul": 0.9},
    })
    monkeypatch.setattr(jev, "decide", decide)
    out = cell_check.check_parties(text, {"owner_name": "Vendor Metals Pty Ltd"}, "Foggy Mountain", "Acme Gold Ltd")
    assert out["action"] == "review" and "proposal" not in out


SPLIT = ("The Company will issue shares to the value of $1.1m to Outback Minerals for the Bonnie Vale and Breakaway Dam "
         "projects, and pay $100,000 cash. ")


def test_a_value_split_across_projects_is_computed_and_not_corrected_to_its_total(monkeypatch):
    decide, asked = fake_jev({
        "shared": lambda s, q: {"type": "noul", "noul": 0.9},
        "correct": lambda s, q: {"type": "noul", "noul": 0.9},
        "role": lambda s, q: {"type": "choice", "choice": "shares_value"},
    })
    monkeypatch.setattr(jev, "decide", decide)
    cell = cell_check.check_cell(SPLIT, "Bonnie Vale Project", "shares_value", 550_000, "AUD", {"status": "NOT_FOUND"},
                                 None, None, {"shares_value": 550_000}, {})
    assert cell["source"] == "computed: 1,100,000 split across 2 projects" and cell["p_shared_between_projects"] == 0.9
    assert cell["action"] == "accept" and "$1.1m" in asked[-1][0]["document_passage"]          # Jev read the total


def test_a_split_that_matches_by_chance_is_not_accepted(monkeypatch):
    # Elizabeth Hill (row 247): 22,250,000 x 2 = the 44,500,000 shares written, but nothing is shared between projects
    text = "The Company will issue 44,500,000 shares to the vendor for the Elizabeth Hill project."
    decide, asked = fake_jev({
        "shared": lambda s, q: {"type": "noul", "noul": 0.05},
        "pick": lambda s, q: {"type": "choice", "choice": "o1", "probabilities": {"o1": 0.91}},
    })
    monkeypatch.setattr(jev, "decide", decide)
    cell = cell_check.check_cell(text, "Elizabeth Hill", "shares_amount", 22_250_000, None, {"status": "NOT_FOUND"},
                                 None, None, {"shares_amount": 22_250_000}, {})
    assert cell["source"] == "not_in_document" and cell["proposal"] == 44_500_000
    assert cell["action"] == "review"           # a share count can be a sum of tranches: the proposal goes to review


def test_an_aggregate_is_the_sum_of_its_parts_and_takes_their_weakest_verdict(monkeypatch):
    monkeypatch.setattr(jev, "decide", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no Jev call for a sum")))
    done = {"cash_payments_value": {"p_correct": 0.9}, "shares_value": {"p_correct": 0.85}}
    cell = cell_check.check_cell("Nothing that says 100,001.", "X", "aggregate_deal_value", 100_000, "AUD", {"status": "DERIVED"},
                                 None, None, {"cash_payments_value": 15_000, "shares_value": 85_000}, done)
    assert cell["source"] == "computed: cash + exploration commitment + shares value"
    assert cell["p_correct"] == 0.85 and cell["action"] == "accept"


def test_market_symbols_and_the_date_window(tmp_path, monkeypatch):
    assert market.symbol("ASX:BCN") == ("BCN.AX", "AUD") and market.symbol("TSXV:ABC") == ("ABC.V", "CAD")
    assert market.symbol("BCN") is None and market.symbol(None) is None
    monkeypatch.setattr(market, "CACHE", tmp_path)

    class Frame:
        """The two things close_on reads from a pandas frame: the dates and the Close column."""

        def __init__(self, rows):
            self.index = [SimpleNamespace(date=lambda d=d: d) for d, _ in rows]
            self._close = [c for _, c in rows]

        def __getitem__(self, name):
            return self._close

    # a ticker that listed months later (Greatland, June 2025) must not give a price for October 2024
    assert market.close_on("ASX:GGP", "2024-10-09", history=lambda s, a, b: Frame([(dt.date(2025, 6, 24), 7.3)])) is None
    found = market.close_on("ASX:BCN", "2024-10-09", history=lambda s, a, b: Frame([(dt.date(2024, 10, 9), 0.88)]))
    assert found == {"symbol": "BCN.AX", "date": "2024-10-09", "close": 0.88, "currency": "AUD"}
