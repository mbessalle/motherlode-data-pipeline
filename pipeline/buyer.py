"""Is the stored owner the buyer? The client's owner is the company buying the asset, and no code can tell a buyer from a
seller, so TypeSafe's Jev decision model answers it with a probability (pipeline/jev.py).

The graph asks it before a row is stored, when OPENROUTER_API_KEY is set: an owner below REVIEW is withheld with
its ticker and queued for review, like an unsupported number. It costs about 0.00004 USD a deal. The same question
and passage are used by the value check in evals/cell_check.py.
"""
from __future__ import annotations

import re

from . import jev
from .search import company_words

REVIEW = 0.5          # below this the owner is withheld
MAX_PASSAGE = 3000

OPENING_CHARS = 1200      # the opening paragraph usually says who acquires what
_DEAL_TERMS = re.compile(r"acqui|purchas|\bsells?\b|\bsale\b|\bsold\b|vendor|option|earn[- ]?in|farm[- ]?(?:in|out)|"
                         r"joint venture|\bjv\b|merger|takeover|consideration|agreement|transfer|divest", re.I)
BUYER = jev.noul(
    "Is named_company the buyer in this transaction: the company that acquires, or can earn, the interest in the project?",
    yes="named_company acquires, purchases, earns into, farms into or takes an option over the project interest, or pays "
        "the consideration for it (cash, its own shares, exploration spending); in a merger or takeover it is the "
        "company that acquires the other.",
    no="named_company is the vendor or seller: it sells, divests, grants the option, farms out, or receives the "
       "payments or shares; or it is a third party (adviser, government, a partner that acquires nothing); or it is "
       "not in the passage.")
ROLE = jev.choice("What is named_company's role in the transaction in document_passage?", {
    "buyer": "Acquires or earns the interest in the project and pays the consideration.",
    "seller": "Sells, options out or farms out the project and receives the consideration.",
    "other_party": "Takes part in some other way: adviser, government, partner that acquires nothing.",
    "not_mentioned": "Does not appear in the passage."})


def deal_passage(text: str, company: str) -> str:
    """The opening of the announcement, plus the sentences that name the company together with a deal term."""
    opening = text[:OPENING_CHARS]
    stop = opening.rfind(". ")
    opening = opening[:stop + 1] if stop > 0 else opening
    words = company_words(company)
    picked = []
    for sentence in re.split(r"(?<=[.!?])\s+", text[len(opening):]):
        low = sentence.lower()
        if words and any(w in low for w in words) and _DEAL_TERMS.search(sentence) and len(sentence) < 600:
            picked.append(sentence)
        if len(picked) == 4:
            break
    return (opening + (" [...] " + " [...] ".join(picked) if picked else ""))[:MAX_PASSAGE]


_ROLE_WORDS = re.compile(r"vendors?\b|optionor|optionee|acquir|purchas|\bsells?\b|\bsale\b|\bsold\b|farm[- ]?(?:in|out)|"
                         r"earn[- ]?in|\bearn\b|transfer|consideration|divest", re.I)


def party_passage(text: str, company: str, limit: int = 3000) -> str:
    """The opening, the sentences naming the company with a deal term, and every sentence with a role word
    ("the vendors", "optionee", "acquire"): who buys is often said only once, without the stored owner's name."""
    out = deal_passage(text, company)
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if len(out) + len(sentence) > limit:
            break
        if _ROLE_WORDS.search(sentence) and len(sentence) < 500 and sentence not in out:
            out += " [...] " + sentence
    return out[:limit]


def check(text: str, owner: str | None, project: str | None) -> dict | None:
    """{'p_buyer': P(owner is the buyer), 'role': buyer / seller / other_party / not_mentioned}, or None without an
    owner. A failed call raises, and the graph then stores the owner unchecked rather than guessing."""
    if not owner:
        return None
    text_passage = party_passage(text, owner)
    answers = jev.decide({"named_company": owner, "project": project or "", "document_passage": text_passage},
                         {"named_company_is_buyer": BUYER, "role": ROLE})
    return {"p_buyer": answers["named_company_is_buyer"]["noul"], "role": answers["role"]["choice"]}
