"""Screening with OpenAI's Decisions API: one call answers "is this a project deal?" and "which continent?".

The chat-based path (llm.triage + llm.classify_location) needs two generative calls for the same two answers.
A decision model returns typed answers with probabilities and no generated text, so it is the natural fit for
yes/no and pick-one questions. It cannot name the project; that still comes from a generative call.
Compared in evals/compare_screening.py.
"""
from __future__ import annotations

import requests

from . import telemetry
from .config import CONTINENT_TO_TABLE

DECISIONS_URL = "https://api.openai.com/v1/decisions"
DECISION_MODEL = "gpt-6-luna"
SCREEN_CHARS = 4000
UNKNOWN = "unknown"

QUESTIONS = [
    {"type": "predicate", "name": "project_deal", "instructions": (
        "Does the announcement report a transaction over a specific mineral project or property: an acquisition, "
        "sale, option, earn-in, farm-in, joint venture, or a merger or takeover that transfers named projects? "
        "It does not when it only reports drilling or assay results, a financing or placement, production or "
        "sales of metal, a shareholder vote, or an operational update.")},
    {"type": "choice", "name": "continent", "instructions": "On which continent is the project in the transaction located?",
     "choices": [{"value": name} for name in CONTINENT_TO_TABLE] + [{"value": UNKNOWN}]},
]


def screen(api_key: str, title: str, company: str, announcement_text: str, post=requests.post,
           threshold: float = 0.5) -> dict:
    """Both screening answers from one request. Raises on an HTTP error; a refusal counts as "no" / unknown."""
    body = {"model": DECISION_MODEL, "questions": QUESTIONS,
            "input": f"Headline: {title}\nCompany: {company}\nAnnouncement (start):\n{announcement_text[:SCREEN_CHARS]}"}
    attributes = {"gen_ai.operation.name": "decide", "gen_ai.provider.name": "openai",
                  "gen_ai.request.model": DECISION_MODEL, "pipeline.llm.purpose": "screen"}
    with telemetry.span(f"decide {DECISION_MODEL}", **attributes) as s:
        response = post(DECISIONS_URL, headers={"Authorization": f"Bearer {api_key}"}, json=body, timeout=60)
        response.raise_for_status()
        data = response.json()
        usage = data.get("usage", {})
        s.set_attribute("gen_ai.usage.input_tokens", usage.get("input_tokens", 0))
        s.set_attribute("gen_ai.usage.output_tokens", usage.get("output_tokens", 0))
    answers = {answer.get("name"): answer for answer in data.get("answers", [])}
    deal, place = answers.get("project_deal", {}), answers.get("continent", {})
    probability = deal.get("probability", 0.0) if deal.get("type") == "predicate" else 0.0
    continent = place.get("choice") if place.get("type") == "choice" else UNKNOWN
    return {"is_project_deal": probability >= threshold, "probability": probability,
            "continent": None if continent == UNKNOWN else continent, "confidence": place.get("confidence"),
            "input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0)}
