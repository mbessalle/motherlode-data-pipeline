# Extraction rules

The rule set written while extracting the historical dataset (about 700 project transactions) that preceded this
pipeline. It is kept here as the domain reference behind the extraction prompt.

- [`core-logic.md`](core-logic.md): the field schema, how buyer and seller are identified, the order of
  precedence for payment values, and the rules for resources, currency, area, royalties and tickers.
- [`edge-cases.md`](edge-cases.md): what was learned from wrong extractions: data for the wrong project, values
  shared between projects, legacy terms from earlier agreements, farm-ins, royalties retained by the seller.

How this relates to the pipeline today:

- The pipeline's prompt ([`pipeline/prompts.py`](../../pipeline/prompts.py)) carries the rules that apply to a
  single announcement, for example "a value shared between N projects is value / N", and the validator
  ([`pipeline/validate.py`](../../pipeline/validate.py)) knows the same rules so it can recognise a derived value.
- These documents use the earlier field names (`cash_payments_raw`, `amount_of_shares_issued`, ...) and ask for a
  quoted justification per field. The current schema is [`pipeline/schema.py`](../../pipeline/schema.py).
- Not yet in the pipeline: a quoted justification for every field, which code could verify against the source.
