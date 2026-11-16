"""Findings of the round-2 critic review (evals/.cache/critic_report_2.md in the review worktree), one test each.
They failed on aed5286 and pass once the finding is fixed."""
from types import SimpleNamespace

from evals import escalate
from pipeline import llm, run, sources
from pipeline import validate as V
from pipeline.store import MemoryStore
from tests.test_run import RELEASE, FakeClient, FakeSearcher, listing, settings_for


# N1 -- a real "no" with "projects": null is a failed call, and the run stops on it every time
def test_a_no_with_null_projects_is_a_no():
    client = FakeClient([{"is_project_deal": False, "project_name": None, "projects": None, "reason": "drilling results"}])
    verdict = llm.triage(client, "m", "t", "c", "text")
    assert "error" not in verdict and verdict["reason"] == "drilling results"


def test_one_bad_article_does_not_block_every_later_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    feed = listing((2, "NewCo Acquires Acme Project"), (1, "NewCo Acquires Zeta Project"))
    bad = {"is_project_deal": True, "project_name": "Zeta", "projects": [""], "reason": "x"}   # malformed every time
    for _ in range(3):
        client = FakeClient([bad, {"is_project_deal": True, "project_name": "Acme", "reason": "p"},
                             {"country": "Canada", "continent": "North America"},
                             {"database_updates": {"cash_payments_value": 100000}}])
        run.run(settings=settings_for(tmp_path), store=MemoryStore({"north_america": []}), client=client,
                searcher=FakeSearcher({1: RELEASE, 2: RELEASE}), fetch=lambda: feed)
    # after repeated failures the article must be reported and passed, or article 2 is never processed
    assert sources.read_state(run.STATE_FILE) == "2"


# N2 -- a failed extraction call (extract_failed) is passed by the state, so the deal is never retried
def test_failed_extraction_is_retried_by_the_next_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    feed = listing((13, "NewCo Acquires Acme Project"))
    client = FakeClient([{"is_project_deal": True, "project_name": "Acme", "reason": "p"},
                         {"country": "Canada", "continent": "North America"}, RuntimeError("429")])
    summary = run.run(settings=settings_for(tmp_path), store=MemoryStore({"north_america": []}), client=client,
                      searcher=FakeSearcher({13: RELEASE}), fetch=lambda: feed)
    assert summary["outcomes"] == {"extract_failed": 1}
    assert sources.read_state(run.STATE_FILE) != "13"


# N3 -- a failed region call is "unclassified" and the row goes to the fallback table, like a real "unknown"
def test_failed_region_call_is_reported_as_a_failure():
    client = FakeClient([RuntimeError("timeout")])
    assert "error" in llm.classify_location(client, "m", "tags: 'Canada'")


# N4 -- a number glued to letters (drill hole BVRB105) is evidence for an area of 105 (row 309 Breakaway Dam)
def test_hole_id_is_not_evidence():
    assert V.find_number("BVRB104 RAB 17 326726 BVRB105 RAB 13", 105) is None


# N5 -- "Figure 2 - Project Location" is read as "2 projects" (row 247 cash 250,000 = half of 500,000)
def test_figure_caption_is_not_a_project_count():
    assert V.stated_project_counts("Figure 2 - Project Location 4 WAMEX Annual Report") == set()
    r = V.validate({"cash_payments_value": 250000},
                   "Consideration: $500,000 cash to Alien. Figure 2 - Project Location. 70% of 3 tenements.")
    assert r.fields["cash_payments_value"].status == V.NOT_FOUND


# N6 -- option counts are summed into a share count (row 247: 11,250,000 + 11,000,000 options = 22,250,000)
def test_option_counts_are_not_a_share_count():
    text = ("Corporate Options to Brokers 11,250,000 Proposed issue of options to Director and related parties "
            "11,000,000 Total Options. Consideration Shares Agreed to be issued 44,500,000")
    assert V.validate({"shares_amount": 22_250_000}, text).fields["shares_amount"].status == V.NOT_FOUND


# N7 -- a share price the model made up (MARKET, never checked in production) makes shares_value and the aggregate DERIVED
def test_unchecked_market_price_does_not_derive_shares_value():
    r = V.validate({"share_price": 0.05, "shares_amount": 1_000_000, "shares_value": 50_000, "cash_payments_value": 20_000,
                    "aggregate_deal_value": 70_000}, "The Company will issue 1,000,000 shares and pay $20,000 cash.")
    assert r.fields["shares_value"].status != V.DERIVED


# N8 -- the metres rule accepts any 4,000 in the text, even "$4,000"
def test_metres_rule_needs_metres():
    r = V.validate({"exp_comit_value": 1_000_000}, "The farmee pays $4,000 per month in rent.")
    assert r.fields["exp_comit_value"].status == V.NOT_FOUND


# N9 -- one row per project: a repeated or contained name makes two rows and halves every value
def test_project_list_has_no_repeats():
    assert run.project_parts({"project_name": "Telfer and Havieron", "projects": ["Telfer", "Telfer"]},
                             "Telfer and Havieron") == ["Telfer"]


# N10 -- Opus's buyer is "quote checked" without its name being in the quote
def test_opus_buyer_name_must_be_in_its_quote():
    text = "Forrestania holds the Option to acquire the project from the vendors, Outback Minerals."
    answer = escalate.check_answer({"cells": [], "buyer": {"name": "Invented Gold Corp",
                                                          "quote": "holds the Option to acquire the project"}}, text)
    assert answer["buyer"]["quote_checked"] is False


# N11 -- Opus's value check reads "4,000m of drilling" as 4,000 million (round-1 F1 again, in the new tier)
def test_opus_value_check_reads_metres_as_metres():
    assert not escalate.value_supported(4_000_000_000, "complete a minimum of 4,000m of drilling", "exp_comit_value")


# the 3-try limit: a call that fails on every run holds progress twice, then the article is passed
def test_a_failing_article_is_passed_after_three_tries(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    feed = listing((2, "NewCo Acquires Acme Project"), (1, "NewCo Acquires Zeta Project"))
    states, outcomes = [], []
    for _ in range(3):
        client = FakeClient([RuntimeError("down"), {"is_project_deal": False, "reason": "drilling"}])
        summary = run.run(settings=settings_for(tmp_path), store=MemoryStore({"north_america": []}), client=client,
                          searcher=FakeSearcher({1: RELEASE, 2: RELEASE}), fetch=lambda: feed)
        states.append(sources.read_state(run.STATE_FILE))
        outcomes.append(summary["outcomes"])
    assert states == ["", "", "2"]
    assert outcomes[:2] == [{"triage_failed": 1}, {"triage_failed": 1}]
    assert outcomes[2] == {"triage_failed_after_3_tries": 1, "not_a_project_deal": 1}


def test_more_date_forms_are_read():
    from pipeline.search import dated_near
    assert dated_near("ASX RELEASE 20-11-2024", "2024-11-19T00:00:00Z")
    assert dated_near("Date: 20/11/24 Perth", "2024-11-19T00:00:00Z")
    assert sources.looks_like_a_deal("ILT granted copper exploration licence adjacent to Mt Lyell")


# round-1 F12 / round-2 N7: production looks the share price up; a count without a price gets price and value
def test_the_share_price_comes_from_the_market_and_confirms_shares_value():
    from types import SimpleNamespace

    from pipeline.graph import Deps, build_graph, process_article
    text = "Acme will issue 2,000,000 shares to the vendor and pay $40,000 cash. " * 30
    store = MemoryStore()
    asked = []

    def market(ticker, day):
        asked.append((ticker, day))
        return {"close": 0.05, "date": day, "symbol": "ACM.AX"}

    app = build_graph(Deps(scrape=lambda url: text, extract=lambda t, r, f: {
        "shares_amount": 2_000_000, "cash_payments_value": 40_000, "owner_ticker": "ASX:ACM", "news_date": "2025-03-04"},
        store=store, review=SimpleNamespace(add=lambda *a, **k: None), market=market))
    final = process_article(app, {"id": 1, "project_name": "P", "link": "u", "title": "t", "date": "2025-03-05T00:00:00Z",
                                  "classification": {"status": "new", "table_name": "oceania"}})
    row = store.fetch_record("oceania", "P")
    assert asked == [("ASX:ACM", "2025-03-04")]
    assert row["share_price"] == 0.05 and row["shares_value"] == 100_000
    assert final["report"]["fields"]["shares_value"]["status"] == "DERIVED"


# round-1 F11: production checks that the owner is the buyer; a seller is withheld with its ticker, for review
def test_an_owner_who_is_not_the_buyer_is_withheld():
    from types import SimpleNamespace

    from pipeline.graph import Deps, build_graph, process_article
    text = "Spartan Resources sells E51/1681 to Fenix Resources for $250,000 cash. " * 30
    store, queued = MemoryStore(), []
    app = build_graph(Deps(scrape=lambda url: text, extract=lambda t, r, f: {
        "cash_payments_value": 250_000, "owner_name": "Spartan Resources", "owner_ticker": "ASX:SPR"},
        store=store, review=SimpleNamespace(add=lambda *a, **k: queued.append(a)),
        buyer=lambda text, owner, project: {"p_buyer": 0.03, "role": "seller"}))
    final = process_article(app, {"id": 1, "project_name": "P", "link": "u", "title": "t", "date": "",
                                  "classification": {"status": "new", "table_name": "oceania"}})
    row = store.fetch_record("oceania", "P")
    assert "owner_name" not in row and "owner_ticker" not in row and row["cash_payments_value"] == 250_000
    assert final["outcome"] == "inserted_partial" and final["withheld"]["owner_name"] == "Spartan Resources"
    assert queued and queued[0][3] == {"owner_name": "Spartan Resources", "owner_ticker": "ASX:SPR"}


def test_a_number_after_an_accented_letter_does_not_crash_the_validator():
    assert V.find_number("Société105 RAB", 105) is None
    assert V.validate({"cash_payments_value": 500_000}, "Paiement é500,000 et A$500,000 en espèces.").passed


# a group with no name of its own: the model gives project_name null and the list (seen on 5 of 30 deals)
def test_a_list_of_projects_without_a_group_name_is_one_row_each():
    source = SimpleNamespace(url="u", text="Newmont sells the Telfer operation and the Havieron project to Greatland. " * 30)
    searcher = SimpleNamespace(find_announcement=lambda a: (source, []))

    def triage(*a):
        return {"is_project_deal": True, "project_name": None, "projects": ["Telfer", "Havieron"], "reason": "sale"}

    article = {"id": 357, "title": "t", "company": "c"}
    stop, details = run.prepare(article, searcher, triage)
    assert stop is None and article["projects"] == ["Telfer", "Havieron"]
    assert article["project_name"] == "Telfer and Havieron"


def test_a_connection_string_gives_the_database_settings():
    from pipeline.config import database_from_url
    assert database_from_url("postgresql://postgres.abc:p%40ss@aws-0-eu.pooler.supabase.com:6543/postgres") == {
        "user": "postgres.abc", "password": "p@ss", "host": "aws-0-eu.pooler.supabase.com", "port": "6543", "dbname": "postgres"}
    assert database_from_url(None) == {}
