"""Pydantic models for what the models return.

`DealExtraction` is the contract between the extraction prompt and the database: the same fields the prompt
lists, with the types, ranges and formats the columns need. A reply that breaks it is not written.
`TriageVerdict` does the same for the screening call.
"""
from __future__ import annotations

from datetime import date
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictBool, ValidationError


def _blank_to_none(value):
    """Strings are trimmed; an empty string means "no value"."""
    if isinstance(value, str):
        return value.strip() or None
    return value


def _number(value):
    """Accept 2000000, 2000000.0, "2,000,000" and "$2,000,000"; reject words and booleans."""
    value = _blank_to_none(value)
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("must be a number or null")
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").replace("$", ""))
        except ValueError:
            raise ValueError("must be a number or null") from None
    return value


Text = Annotated[str | None, BeforeValidator(_blank_to_none)]
Number = Annotated[float | None, BeforeValidator(_number)]
# A range or pattern belongs to the value, not to the "value or null" union, so null stays valid.
Percent = Annotated[Annotated[float, Field(ge=0, le=100)] | None, BeforeValidator(_number)]
Longitude = Annotated[Annotated[float, Field(ge=-180, le=180)] | None, BeforeValidator(_number)]
Latitude = Annotated[Annotated[float, Field(ge=-90, le=90)] | None, BeforeValidator(_number)]
Currency = Annotated[Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None, BeforeValidator(_blank_to_none)]


class DealExtraction(BaseModel):
    """One project transaction, as the extraction prompt defines it. Unknown keys are dropped."""

    model_config = ConfigDict(extra="ignore")

    project_name: Text = None
    primary_commodity: Text = None
    commodities: Text = None
    resource_value_scraped: Text = None
    stage: Text = None
    stage_mining_hub: Text = None
    stage_scrapped: Text = None
    project_state_or_province: Text = None
    project_country: Text = None
    longitude: Longitude = None
    latitude: Latitude = None
    project_area: Number = None
    project_area_unit: Text = None
    owner_name: Text = None
    owner_ticker: Text = None
    owner_marketcap: Number = None
    news_link: Text = None
    news_title: Text = None
    deal_type: Text = None
    deal_period: Text = None
    interest_acquired_percent: Percent = None
    aggregate_deal_currency: Currency = None
    aggregate_deal_value: Number = None
    cash_payments_currency: Currency = None
    cash_payments_value: Number = None
    exp_comit_currency: Currency = None
    exp_comit_value: Number = None
    drilling_amount: Number = None
    drilling_value: Number = None
    shares_currency: Currency = None
    shares_value: Number = None
    shares_value_scraped: Number = None
    shares_amount: Number = None
    share_price: Number = None
    nsr_percent: Percent = None
    news_date: Annotated[date | None, BeforeValidator(_blank_to_none)] = None


class TriageVerdict(BaseModel):
    """The screening call's reply. Only a real boolean counts: "yes" is not true."""

    model_config = ConfigDict(extra="ignore")

    is_project_deal: StrictBool = False
    project_name: Text = None
    reason: Text = None


def issues(extraction: dict) -> list[str]:
    """Why the extraction breaks the schema, one line per field; empty when it is valid."""
    try:
        DealExtraction.model_validate(extraction)
        return []
    except ValidationError as error:
        return [f"{'.'.join(str(part) for part in e['loc'])}: {e['msg']} (got {e['input']!r})" for e in error.errors()]


def normalise(extraction: dict) -> dict:
    """The extraction with the schema's types applied: numeric strings become numbers, blanks and nulls are
    left out, unknown keys are dropped. Call only on an extraction that has no issues."""
    return DealExtraction.model_validate(extraction).model_dump(mode="json", exclude_none=True)
