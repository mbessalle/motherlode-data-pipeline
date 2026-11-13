"""The Decisions-API screening call, with the HTTP layer replaced by a function."""
from types import SimpleNamespace

from pipeline import decisions


def reply(answers, usage=None):
    def post(url, headers, json, timeout):
        post.sent = {"url": url, "headers": headers, "json": json}
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"model": "gpt-6-luna", "answers": answers, "usage": usage or {"input_tokens": 357, "output_tokens": 0}})
    return post


def test_one_request_carries_both_questions_and_the_trimmed_announcement():
    post = reply([{"type": "predicate", "name": "project_deal", "probability": 0.97},
                  {"type": "choice", "name": "continent", "choice": "North America", "confidence": 0.99}])
    result = decisions.screen("key", "Fuse Options Foggy Mountain", "fuse battery metals inc", "x" * 9000, post=post)
    assert result == {"is_project_deal": True, "probability": 0.97, "continent": "North America", "confidence": 0.99,
                      "input_tokens": 357, "output_tokens": 0}
    sent = post.sent["json"]
    assert post.sent["url"].endswith("/v1/decisions") and post.sent["headers"] == {"Authorization": "Bearer key"}
    assert [q["type"] for q in sent["questions"]] == ["predicate", "choice"] and sent["model"] == "gpt-6-luna"
    assert [c["value"] for c in sent["questions"][1]["choices"]] == ["Africa", "Asia", "Europe", "North America", "Oceania",
                                                                    "South America", "unknown"]
    assert sent["input"].count("x") == decisions.SCREEN_CHARS


def test_low_probability_unknown_continent_and_refusals_are_handled():
    low = decisions.screen("k", "t", "c", "text", post=reply([{"type": "predicate", "name": "project_deal", "probability": 0.2},
                                                                {"type": "choice", "name": "continent", "choice": "unknown", "confidence": 0.6}]))
    assert low["is_project_deal"] is False and low["continent"] is None
    refused = decisions.screen("k", "t", "c", "text", post=reply([{"type": "refusal", "name": "project_deal"},
                                                                    {"type": "refusal", "name": "continent"}]))
    assert refused["is_project_deal"] is False and refused["continent"] is None and refused["probability"] == 0.0
