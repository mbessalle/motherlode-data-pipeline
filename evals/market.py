"""The buyer's share price on the news date, for a share price the announcement does not state.

The client defines share price as market data: "idealmente valor de la fecha de la noticia, si muy complicado
del dia de scrap" (field sheet, 5 Sep 2025). This reads the daily close with yfinance (Yahoo Finance; unofficial
endpoints, so a lookup can fail, and then the cell goes to review). Only a close within MAX_DAYS calendar days
after the news date is accepted: a ticker that listed later (GGP.AX, listed June 2025) must not return a price
from months afterwards. Results are cached in evals/.cache/market/.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

CACHE = Path(__file__).parent / ".cache" / "market"
MAX_DAYS = 5
SUFFIX = {"ASX": (".AX", "AUD"), "TSXV": (".V", "CAD"), "TSX-V": (".V", "CAD"), "TSX": (".TO", "CAD"), "CSE": (".CN", "CAD"),
          "NYSE": ("", "USD"), "NASDAQ": ("", "USD"), "LSE": (".L", "GBP"), "AIM": (".L", "GBP"), "JSE": (".JO", "ZAR")}


def symbol(ticker: str | None) -> tuple[str, str] | None:
    """'ASX:BCN' or 'ASX.BCN' -> ('BCN.AX', 'AUD'). A ticker without a known exchange gives None."""
    if not ticker:
        return None
    parts = ticker.replace(" ", "").replace(".", ":", 1).split(":")
    if len(parts) != 2 or parts[0].upper() not in SUFFIX or not parts[1]:
        return None
    suffix, currency = SUFFIX[parts[0].upper()]
    return parts[1].upper() + suffix, currency


def close_on(ticker: str | None, day: str | None, history=None) -> dict | None:
    """{'symbol', 'date', 'close', 'currency'} for the first trading day on or up to MAX_DAYS after `day`, or None."""
    found = symbol(ticker)
    if not found or not day:
        return None
    sym, currency = found
    path = CACHE / f"{sym}_{day}.json"
    if path.exists():
        return json.loads(path.read_text()) or None
    start = dt.date.fromisoformat(day[:10])
    if history is None:
        import yfinance

        def history(s, a, b):
            return yfinance.Ticker(s).history(start=a, end=b, auto_adjust=False)
    try:
        frame = history(sym, start.isoformat(), (start + dt.timedelta(days=MAX_DAYS + 1)).isoformat())
    except Exception:          # an unofficial endpoint: a failure means "no price", never a guess
        return None
    result = None
    for stamp, close in zip(frame.index, frame["Close"], strict=True):
        if 0 <= (stamp.date() - start).days <= MAX_DAYS:
            result = {"symbol": sym, "date": stamp.date().isoformat(), "close": round(float(close), 6), "currency": currency}
            break
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result or {}))
    return result
