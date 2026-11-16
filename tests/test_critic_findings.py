"""Findings of the end-to-end critic review (seed 7, 30 analyst deals; .claude/agents/pipeline-critic.md), one test
each. They failed on commit ae96b67 and pass once the finding is fixed. Two were changed from the critic's version
on purpose: owner_marketcap is market data like share price (MARKET, not NOT_FOUND), and an area in a unit that
is not an area withholds the area instead of blocking the whole row as a schema issue.
"""
from types import SimpleNamespace

from pipeline import llm, schema, sources
from pipeline import validate as V
from pipeline.graph import Deps, build_graph, process_article
from pipeline.search import dated_near, release_date
from pipeline.store import MemoryStore

# F1 -- split rule does not cover shares_value (rows 357, 309)
SPLIT_TEXT = ("The option covers 2 project areas: Bonnie Vale and Breakaway Dam. Revised terms: deferral of the "
              "$1.1m share consideration payable up until May 2026. Payment of $100,000 cash upon exercise.")


def test_shares_value_split_across_projects_is_derived():
    r = V.validate({"cash_payments_value": 50000, "shares_value": 550000, "aggregate_deal_value": 600000}, SPLIT_TEXT)
    assert r.fields["cash_payments_value"].status == V.DERIVED          # passes today
    assert r.fields["shares_value"].status == V.DERIVED                 # fails today: NOT_FOUND
    assert r.fields["aggregate_deal_value"].status == V.DERIVED         # fails today: NOT_FOUND


# F2 -- metres read as millions ("1m @ 6.15 g/t" is evidence for 1,000,000)
def test_metre_intercept_is_not_a_million():
    text = "Surface trenching includes 4m @ 3.09g/t Au and 1m @ 6.15 g/t Au and 3.1% Sb."
    assert V.find_number(text, 1_000_000, kind=V.MONEY) is None
    r = V.validate({"exp_comit_value": 3_000_000}, text + " 3m @ 1.2 g/t Au.")
    assert r.fields["exp_comit_value"].status == V.NOT_FOUND


# F3 -- a failed or malformed triage call looks like a real "no"
class _Raises:
    chat = SimpleNamespace(completions=SimpleNamespace(
        create=lambda **k: (_ for _ in ()).throw(RuntimeError("429 rate limit"))))


def test_failed_triage_is_not_reported_as_a_no():
    verdict = llm.triage(_Raises(), "m", "Acquires X Project", "Co", "text")
    # a person reading the run report must be able to tell this from the model saying "no"
    assert verdict != {"is_project_deal": False, "project_name": None, "reason": None}
    assert verdict.get("error") or verdict.get("reason")


# F4 -- a failed retry discards the supported values of attempt 1
def test_failed_retry_keeps_first_attempt():
    calls = []

    def extract(text, record, feedback):
        calls.append(feedback)
        if len(calls) == 1:
            return {"cash_payments_value": 100000, "shares_value": 999}   # 999 is unsupported -> retry
        return {}                                                          # the retry call fails

    store = MemoryStore()
    review = SimpleNamespace(add=lambda *a, **k: None)
    app = build_graph(Deps(scrape=lambda url: "Payment of $100,000 cash on signing. " * 60, extract=extract,
                           store=store, review=review, max_attempts=2))
    final = process_article(app, {"id": 1, "project_name": "P", "link": "u", "title": "t", "date": "",
                                  "classification": {"status": "new", "table_name": "oceania"}})
    assert final["outcome"] != "no_data"
    assert store.fetch_record("oceania", "P") and store.fetch_record("oceania", "P")["cash_payments_value"] == 100000


# F5 -- headline filter drops real deals of the analyst's table (seed 7: 13 of 30)
def test_headline_filter_keeps_analyst_deals():
    for title in ["Agreement Over Advanced Antimony Gold Project - Amendment",
                  "Agreement with Mark Creasy over Peninsula Prospect - Amended",
                  "Kali Expands Pilbara Gold Tenure", "Lode Secures Strategic Antimony Prospects",
                  "Completion of Tenement Addition and Drilling Update", "New tenements expand Kanowna Gold Project",
                  "Significant Increase in Pilbara Landholding", "Trinex Expands Lithium Portfolio into South Australia"]:
        assert sources.looks_like_a_deal(title), title


# F6 -- date formats the date check and release_date cannot read (row 248: "ASX ANNOUNCEMENT 20/11/2024")
def test_numeric_and_padded_dates_are_read():
    assert dated_near("ASX ANNOUNCEMENT 20/11/2024 ASX:TAR", "2024-11-19T00:00:00Z")
    assert dated_near("ASX Release 04 March 2025", "2025-03-04T00:00:00Z")
    assert release_date("ASX ANNOUNCEMENT 20/11/2024", "2024-11-19T00:00:00Z", "2024-11-20") == "2024-11-20"


# F7 -- numeric fields stored without any check
def test_every_stored_number_is_checked():
    r = V.validate({"shares_value_scraped": 83_750_000, "drilling_value": 7_000_000, "owner_marketcap": 1234567},
                   "A text that states none of these numbers. " * 40)
    assert set(r.not_found) >= {"shares_value_scraped", "drilling_value"}
    assert r.fields["owner_marketcap"].status == V.MARKET          # market data: checked against the market, not the text


# F8 -- project_area stored in any unit (Striker: 33 "sub-blocks"; Thowagee 31.5 "km2"; client column is ha)
def test_area_is_normalised_to_hectares():
    row = schema.normalise({"project_area": 31.5, "project_area_unit": "km2"})
    assert (row["project_area"], row["project_area_unit"]) == (3150.0, "Ha")
    report = V.validate({"project_area": 33, "project_area_unit": "sub-blocks"}, "The tenement covers 33 sub-blocks.")
    assert report.not_found == ["project_area"] and not report.schema_issues   # only the area is withheld


# F9 -- share price is market data for the client; a price not written in the release is withheld
def test_market_share_price_is_not_withheld():
    text = "The Company will issue 10,000,000 fully paid ordinary shares to the vendor. " * 20
    r = V.validate({"shares_amount": 10_000_000, "share_price": 0.05, "shares_value": 500_000}, text)
    assert "share_price" not in r.not_found                      # market data never blocks
    # shares_value from that price counts only once code confirmed the price against the market (round-2 N7)
    assert "shares_value" in r.not_found
    close = {"close": 0.051, "date": "2025-03-04", "symbol": "ACM.AX"}
    confirmed = V.validate({"shares_amount": 10_000_000, "share_price": 0.05, "shares_value": 500_000}, text, market_price=close)
    assert confirmed.fields["shares_value"].status == V.DERIVED and confirmed.passed


# F14 -- the identity test counts listing-site verbs as identifying words (row 438: {'sarama','venn','advances'})
def test_listing_verbs_are_not_identifying_words():
    from pipeline.search import distinctive_words
    for verb in ("advances", "expands", "increases", "secures"):
        assert verb not in distinctive_words(f"Sarama Resources {verb.title()} Mt Venn Gold Project Acquisition")


# F11 -- a triage "yes" with no project name is reported as "project_name_not_in_source" (rows 306, 419)
def test_missing_project_name_has_its_own_outcome():
    from pipeline import run as R
    src = SimpleNamespace(url="u", text="x" * 2000, how="pdf")
    searcher = SimpleNamespace(find_announcement=lambda a: (src, []))
    def triage(*a):
        return {"is_project_deal": True, "project_name": None, "reason": "a group of properties"}

    stop, _ = R.prepare({"id": 1, "title": "t", "company": "c"}, searcher, triage)
    assert stop != "project_name_not_in_source"


# F6 -- a deal over "A and B" is one row per project (client: one row per project, shared values split equally)
def test_a_group_name_becomes_one_row_per_project():
    from pipeline.run import project_parts
    text = "Greatland will acquire the Telfer gold-copper mine and the Havieron project from Newmont."
    # the triage model lists the projects; code checks that every listed name is written
    assert project_parts({"project_name": "Telfer and Havieron", "projects": ["Telfer", "Havieron"]}, text) == ["Telfer", "Havieron"]
    assert project_parts({"project_name": "Lord and Lady Hill", "projects": ["Lord and Lady Hill"]},
                         "the Lord and Lady Hill project") == ["Lord and Lady Hill"]
    # a listed name that is not in the text: the list is not trusted, the deal stays one row
    assert project_parts({"project_name": "Telfer", "projects": ["Telfer", "Invented"]}, text) == ["Telfer"]
    assert project_parts({"project_name": "Foggy Mountain", "projects": []}, text) == []


# F15 and row 308 -- a split needs a stated number of projects, and a sum needs its terms written near each other
def test_a_split_needs_a_number_of_projects_and_a_sum_needs_nearby_terms():
    one_project = "Infinity will pay $50,000 cash for 100% of Highland Resources. " * 3
    assert V.validate({"cash_payments_value": 12_500}, one_project).fields["cash_payments_value"].status == V.NOT_FOUND
    four = "Infinity will pay $50,000 cash for the four projects. " * 3
    assert V.validate({"cash_payments_value": 12_500}, four).fields["cash_payments_value"].status == V.DERIVED
    assert V.validate({"cash_payments_value": 25_000}, one_project, project_count=2).fields["cash_payments_value"].status == V.DERIVED
    far_apart = "A payment of $100,000 on signing. " + "Drilling and assays. " * 200 + "Placement of $150,000 completed."
    assert V.validate({"cash_payments_value": 250_000}, far_apart).fields["cash_payments_value"].status == V.NOT_FOUND
    near = "A payment of $100,000 on signing and $150,000 after six months."
    assert V.validate({"cash_payments_value": 250_000}, near).fields["cash_payments_value"].status == V.DERIVED
