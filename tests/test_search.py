"""Finding the announcement: a search result is accepted on evidence in the page, never on its rank."""
from types import SimpleNamespace

from pipeline import sources
from pipeline.search import Searcher, company_words, dated_near, distinctive_words, is_the_announcement

RELEASE = ("VANCOUVER, BC, Nov. 10, 2026 /CNW/ - Fuse Battery Metals Inc. is pleased to announce it has entered into an "
           "option agreement to acquire up to an 80% interest in the Foggy Mountain copper-gold-silver project in British "
           "Columbia. " + "The Company will make staged cash payments and complete drilling on the property. " * 20)
ARTICLE = {"id": 7, "title": "Fuse Options Foggy Mountain Copper-Gold Project in BC", "company": "fuse battery metals inc",
           "date": "2026-11-11T02:00:00.000Z", "listing_link": "https://mininghub.com/company/fuse-battery-metals-inc/insights/x-7"}

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
<url><loc>https://mininghub.com/company/fuse-battery-metals-inc/insights/fuse-options-foggy-mountain-copper-gold-project-in-bc-16378915</loc>
<news:news><news:publication><news:name>Mining Hub</news:name><news:language>en</news:language></news:publication>
<news:publication_date>2026-11-11T02:00:00.000Z</news:publication_date><news:title>Fuse Options Foggy Mountain Copper-Gold Project in BC</news:title></news:news></url>
<url><loc>https://mininghub.com/company/acme-gold-corp/insights/acme-drills-10-m-of-5-g-t-gold-16379000</loc>
<news:news><news:publication_date>2026-11-12T09:00:00.000Z</news:publication_date><news:title>Acme Drills 10 m of 5 g/t Gold &amp; More</news:title></news:news></url>
<url><loc>https://mininghub.com/about</loc></url>
</urlset>"""


def test_news_sitemap_is_parsed_newest_first():
    articles = sources.parse_news_sitemap(FEED)
    assert [a["id"] for a in articles] == [16379000, 16378915]            # the /about entry is not an article
    assert articles[1]["company"] == "fuse battery metals inc" and articles[1]["title"].startswith("Fuse Options")
    assert articles[0]["title"] == "Acme Drills 10 m of 5 g/t Gold & More"


def test_headline_filter_keeps_transactions_and_drops_the_rest():
    keep = ["Fuse Options Foggy Mountain Copper-Gold Project in BC", "Bold Ventures Completes Sale of 42 Burchell Claims",
            "Omineca Secures 50% Wingdam JV Deal With HMI Mining", "Altius Agrees to Acquire TNR Gold in $122M Deal",
            "Thunderbird Completes Acquisition of Option on Big Onion Property", "Arrow enters earn-in on Pannawonica"]
    drop = ["Acme Drills 10 m of 5 g/t Gold", "Quarterly Activities Report", "Beta Closes C$5M Private Placement",
            "Gamma Appoints New Chief Financial Officer"]
    assert all(sources.looks_like_a_deal(t) for t in keep) and not any(sources.looks_like_a_deal(t) for t in drop)


def test_identity_uses_names_and_numbers_not_generic_words():
    assert company_words("fuse battery metals inc") == ["fuse", "battery"]
    assert distinctive_words("Fuse Options Foggy Mountain Copper-Gold Project in BC") == {"fuse", "foggy", "mountain"}
    assert distinctive_words("Altius Agrees to Acquire TNR Gold in $122M Deal") == {"altius", "tnr", "122"}


def test_the_right_release_is_accepted_and_lookalikes_are_not():
    assert is_the_announcement(RELEASE, ARTICLE["title"], ARTICLE["company"], ARTICLE["date"])[0]
    assert is_the_announcement("Fuse Battery Metals " * 3, ARTICLE["title"], ARTICLE["company"])[1] == "too short"
    other_company = RELEASE.replace("Fuse Battery Metals", "Acme Gold")
    assert "company not named" in is_the_announcement(other_company, ARTICLE["title"], ARTICLE["company"])[1]
    other_project = RELEASE.replace("Foggy Mountain", "Clear Lake").replace("option agreement", "letter").replace("British Columbia", "Ontario")
    assert not is_the_announcement(other_project, ARTICLE["title"], ARTICLE["company"], ARTICLE["date"])[0]
    old_document = RELEASE.replace("Nov. 10, 2026", "April 2, 2026")       # same company and project, months earlier
    ok, why = is_the_announcement(old_document, ARTICLE["title"], ARTICLE["company"], ARTICLE["date"])
    assert not ok and "not dated near" in why


def test_date_window_and_spellings():
    listed = "2026-11-11T02:00:00.000Z"
    for written in ("November 10, 2026", "Nov 11, 2026", "8 November 2026", "2026-11-12", "November 9th, 2026", "October 28, 2026"):
        assert dated_near(f"dateline {written} text", listed), written
    for written in ("October 27, 2026", "November 13, 2026", "November 10, 2025", "November 110, 2026"):
        assert not dated_near(f"dateline {written} text", listed), written


def test_searcher_reads_results_in_order_and_reports_what_it_tried():
    pages = {"https://newswire.test/fuse": RELEASE, "https://blog.test/other": "Unrelated text about markets. " * 80}

    def post(url, json, headers, timeout):
        assert headers["X-API-KEY"] == "key" and "Fuse Options" in json["q"]
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"organic": [
            {"link": "https://mininghub.com/company/fuse/insights/x-7"},       # the listing site itself is skipped
            {"link": "https://blog.test/other"}, {"link": "https://newswire.test/fuse"}]})

    def get(url, headers, timeout):
        return SimpleNamespace(status_code=200, headers={"content-type": "text/html"},
                               text=f"<html><body><article><p>{pages[url]}</p></article></body></html>")

    searcher = Searcher("key", post=post, get=get)
    source, tried = searcher.find_announcement(dict(ARTICLE))
    assert source.url == "https://newswire.test/fuse" and "Foggy Mountain" in source.text and searcher.searches == 1
    assert [t["url"] for t in tried] == ["https://blog.test/other", "https://newswire.test/fuse"]
    assert "company not named" in tried[0]["verdict"]
