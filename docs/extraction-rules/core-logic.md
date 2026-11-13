# Rule: M&A Data Extraction - Core Logic
# Activation: Always On
# Description: Contains the primary schema, entity resolution, and core logic for extracting raw data from mining M&A articles. This file is supplemented by extraction_rules_edge_cases.md.

<context>
You are provided with a single, large block of text that has been consolidated from one or more source documents (`.txt` files) related to a single project.

Your prompt will explicitly state the **primary project of interest** and will provide you with **authoritative metadata** (e.g., project name, company name, primary commodity, ticker). You MUST prioritize this authoritative metadata unless the document text provides a clear and direct contradiction.

The combined text block may contain one or more metadata headers formatted like this:
--- Article Metadata ---
Project Name: ...
Company Name: ...
...
------------------------

This header provides context but should be considered secondary to the authoritative metadata given in your prompt. The main content for extraction follows this header.
</context>

<schema>
# CRITICAL: For EACH field, you must provide a `value` and a `justification`.
# The `justification` MUST be a direct quote or very close paraphrase from the source text that proves the `value`.
# If a value is null because no information exists, the justification can be null. If a value is null because a candidate value was REJECTED based on a rule (e.g., wrong project), the justification MUST explain the rejection with a supporting quote.
# If the article link is NOT about the project, the `verified_article_link` field MUST be null, and the justification MUST explain why the article is not about the project and what project it is about.
**In addition, you MUST append a line to `incorrect-projects.txt` with the following tab-separated fields:**
`<article_link> <expected_project_name> <actual_project_name>`
This list is used for QA and model retraining.

{
  "project_name": { "value": "string | null", "justification": "string | null" },
  "company_name": { "value": "string | null", "justification": "string | null" },
  "ceo_buyer": { "value": "string | null", "justification": "string | null" },
  "interest_acquired_percent": { "value": "number | null", "justification": "string | null" },
  "primary_commodity": { "value": "string | null", "justification": "string | null" },
  "verified_article_link": { "value": "string | null", "justification": "string | null" },
  "currency": { "value": "string | null", "justification": "string | null" },
  "cash_payments_raw": { "value": "number | null", "justification": "string | null" },
  "share_payments_raw": { "value": "number | null", "justification": "string | null" },
  "cash_and_share_payments_combined_raw": { "value": "number | null", "justification": "string | null" },
  "amount_of_shares_issued": { "value": "number | null", "justification": "string | null" },
  "share_warrant_payments_raw": { "value": "number | null", "justification": "string | null" },
  "option_payments_raw": { "value": "number | null", "justification": "string | null" },
  "issued_share_price": { "value": "number | null", "justification": "string | null" },
  "exploration_commitment_meters": { "value": "number | null", "justification": "string | null" },
  "exploration_commitment_value_raw": { "value": "number | null", "justification": "string | null" },
  "nsr_acquired_percent": { "value": "number | null", "justification": "string | null" },
  "coverage_area_raw": { "value": "number | null", "justification": "string | null" },
  "coverage_area_unit": { "value": "string | null", "justification": "string | null" },
  "resource_size": { "value": "string | null", "justification": "string | null" },
  "buyer_ticker_and_exchange": { "value": "string | null", "justification": "string | null" }
}
</schema>

---
<entity_resolution_protocol>
- **Identify Roles First:** Before any extraction, you must perform these steps:
  1. Identify the Acquiring Company (`BUYER`). Keywords: "acquire", "purchase", "invest in", "farm-in".
  2. Identify the Target/Selling Company (`SELLER`).
  3. The `ceo_buyer` and `buyer_ticker_and_exchange` fields MUST belong to the `BUYER` entity.
</entity_resolution_protocol>

---
<extraction_logic>
- **PRINCIPLE:** RAW DATA ONLY. You must not perform any calculations or unit conversions. Your job is to find the raw numbers and labels.

- **Financial Data Extraction Hierarchy (IMPORTANT & MUTUALLY EXCLUSIVE):**
  - **PRIORITY 1 (Explicit Values):** If text has separate cash/share values, populate `cash_payments_raw` & `share_payments_raw`. Other financial fields null.
  - **PRIORITY 2 (Calculable Share Value):** If text has the number of shares and a price per share for the transaction, populate `amount_of_shares_issued` & `issued_share_price`. Other financial fields null.
    - **################ NEW FALLBACK RULE ################**
    - **Fallback Rule for `issued_share_price`:** If `amount_of_shares_issued` can be populated from the deal terms but an explicit `issued_share_price` **cannot** be found in the main article body, you MUST use the `Last` price from the "Detailed Quote" section as the value for `issued_share_price`.
    - The `justification` for this fallback MUST explicitly state that the last traded price is being used.
    - **Example Fallback Justification:** `"justification": "The transaction terms mention the number of shares issued, but not the price. Using the last traded price as a fallback, as seen in the detailed quote: 'Last: 0.035'."`
    - **#####################################################**
  - **PRIORITY 3 (Combined Value):** If text has one combined value, populate `cash_and_share_payments_combined_raw`. Other financial fields null.

- **Rule for `resource_size` (ULTRA-CRITICAL - FOLLOW PRECISELY):**
  - **Purpose:** This field is ONLY for a quantified statement of mineral resources.
  - **Step 1: IDENTIFY A QUANTITY.** First, find a sentence that states a mineral resource. This sentence MUST contain numbers for mass or volume (e.g., 'tonnes', 'ounces', 'pounds', 'Mt', 'Moz').
  - **Step 2: HANDLE NEGATIVE CASES.** If the text explicitly states that **NO resource exists** (e.g., "No previous Mineral Resources exist", "no current resource"), then the `value` for this field MUST be `null`. The `justification` MUST quote the sentence proving there is no resource. THIS IS A MANDATORY SUB-RULE.
    - **Example Negative Case:**
      - Text: "No previous or historical Mineral Resources or Ore Reserves exist."
      - Correct Output: `"value": null`, `"justification": "The text explicitly states: 'No previous or historical Mineral Resources or Ore Reserves exist.'"`
  - **Step 3: EXTRACT THE SUB-STRING.** If a resource quantity IS found, the `value` MUST be **ONLY the specific measurement part of the sentence.** You MUST NOT include introductory phrases like "The project has a resource of..." or "This includes an estimate of...". Your extraction should start with the number.
    - **CORRECT (Starts with a number):** `710 million tonnes of vanadium mineral resource, at 0.46 V2O5 (wt%)`
    - **INCORRECT (Includes intro phrase):** `Inferred JORC Resource of 710 million tonnes...`
    - **CORRECT:** `60Mt @ 1.2g/t Au for 2.3Moz`
    - **INCORRECT:** `A Mineral Resource estimate comprising 60Mt @ 1.2g/t Au for 2.3Moz`
  - **UNIT TYPE CHECK (FINAL VALIDATION):** Before finalizing, confirm the value is a **MASS or VOLUME**. Any value describing a **LENGTH** (meters, km of drilling) or **AREA** (ha, sq km) is **STRICTLY FORBIDDEN** in this field and MUST result in `null`.

- **CEO:** The `value` must be the person's full name only. NO titles (CEO, Chair) or punctuation (,-) are allowed.
- **Currency:** The `value` must be the three-letter ISO code (AUD, CAD, USD). Infer the currency from context (e.g., ASX listed company implies AUD) if only a generic "$" symbol is used.
- **Area:** Extract the number to `coverage_area_raw` and the unit (e.g., "ha", "km2") to `coverage_area_unit`. Do NOT perform the conversion.
- **Exploration:** If the commitment is in meters, populate `exploration_commitment_meters`. If it is a monetary value, populate `exploration_commitment_value_raw`. These two fields are mutually exclusive.
- **Interest/NSR:** The `value` must be the numeric value only. "51% interest" becomes `51`.
- **Royalty Types:** Treat "Gross Revenue Royalty" (GRR) as equivalent to "Net Smelter Royalty" (NSR). Extract the value into the `nsr_acquired_percent` field.
- **Buyer Ticker and Exchange:** To find the `buyer_ticker_and_exchange`, follow this logic to produce the final string:
  0.  **Authoritative Metadata Override:** Prioritize the `exchange` and `ticker` provided as authoritative metadata in the prompt. Use these unless the text provides a clear, contradictory ticker for the identified `BUYER`.
  1.  **Highest Priority (Text Supersedes):** Look for the full `EXCHANGE:TICKER` or `EXCHANGE.TICKER` format in parentheses after the buyer's name, e.g., `(ASX:NPM)` or `(ASX.INF)`. Extract this value directly.
  2.  **Secondary Priority:** If only a ticker is in parentheses, e.g., `(C7A)`, scan the document for the exchange name (e.g., "ASX Announcement") and combine them into `EXCHANGE:TICKER` format.
  3.  **Tertiary Priority:** If no ticker is in parentheses, look for a three-letter, all-caps abbreviation defined with the company name, e.g., `Duketon Mining Limited (Company or DKM)`. Combine this `DKM` ticker with the exchange name from the document header to create `EXCHANGE:TICKER` format.
  4.  **Fallback:** If an exchange cannot be found using the methods above, extract only the ticker symbol itself.
</extraction_logic>