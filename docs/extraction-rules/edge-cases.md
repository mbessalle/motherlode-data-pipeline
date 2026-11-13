# Rule: M&A Data Extraction - Supplement 1 (Edge Cases & Learnings)
# Activation: Always On
# Description: Contains specific, learned edge cases and data hygiene rules that supplement the core extraction logic.

<edge_cases_and_learnings>

- **RULE #1: DATA RELEVANCE (THE MOST IMPORTANT RULE OF ALL).**
  - *Example of incorrect project identification:* The article in a file might actually describe the acquisition of the **Elko** project, even though it's located in the `Blue_Clay` folder.
    - Action: `verified_article_link` must be `null` with justification explaining it refers to Elko.
    - Add line to `incorrect-projects.txt`: `https://<article-url>\tBlue_Clay\tElko`
  - **PRINCIPLE:** Before extracting ANY value, you must first determine which project the data point belongs to. Your prompt will name the primary project of interest. Your final output MUST ONLY contain data for THAT project.
  - **IF MULTIPLE PROJECTS ARE MENTIONED:** You MUST find the data points specifically associated with the primary project of interest.
    - **Example:** If the document says "The deal includes the 100km2 Bingara project and the 50km2 Nundle project for a total of 150km2," and you are processing for **Bingara**, the correct `coverage_area_raw` is `100`, NOT `150`.
  - **JUSTIFICATION FOR IGNORED DATA:** When a potential value (like a total area or a commitment for another project) is ignored due to this rule, the `justification` for the field MUST explicitly state that the data was for the wrong project or was an ignored total, and MUST name the other project(s) or explain why the total was ignored. This applies to every field where a candidate value was considered and rejected.

- **SHARED COMMITMENTS & VALUES (SUB-RULE of RULE #1):**
  - **CRITICAL:** If a monetary value or commitment (like `exploration_commitment_value_raw`) is explicitly stated to be shared between N projects, the value you extract for the primary project MUST be `RAW_VALUE / N`.
  - **Your `justification` MUST show this calculation.**
  - **Example:**
    - Text: "a commitment to spend $1,000,000 across the Bingara and Nundle projects." (N=2 projects)
    - Project of Interest: **Bingara**
    - Correct `exploration_commitment_value_raw`: `500000`
    - Correct `justification`: "The project's share is $500,000, calculated as half of the $1,000,000 commitment shared between the Bingara and Nundle projects as stated in the text: '...spend at least $1,000,000 of exploration expenditure on the projects...'"

- **Scan Appendices & JORC Tables:** Crucial data, especially `coverage_area_raw`, is often hidden in appendices like "JORC Code - Table 1". You must scan the 'Commentary' column of these tables.

- **Distinguish Current vs. Historical/Legacy Terms:** Focus your extraction on the "Consideration" or "Transaction Terms" of the NEW deal being announced. You must ignore financial details described as "legacy," "historical," or from a prior agreement that is now being completed or superseded.

- **Cash-for-Equity vs. Share Swaps:** If Company A pays cash to Company B to receive shares *in Company B*, this is a `cash_payments_raw` from Company A's perspective. If Company A pays by giving shares *of its own stock (Company A stock)*, it is a `share_payments_raw`.

- **Aggregate All Payment Components (CRITICAL):** You must sum all forms of a payment type into a single total. This includes future, conditional, or milestone-based payments.
  - **Example:** If a deal is for "$250,000 cash and a milestone payment of $1,000,000", the `cash_payments_raw` value MUST be `1250000`.

- **NSR in Transaction:** The `nsr_acquired_percent` field should be populated if an NSR is part of the transaction's terms. This includes both NSRs *acquired* by the buyer and NSRs *retained* by the seller. The justification must clarify whether the NSR was acquired or retained.

- **Farm-in Agreements:** For `interest_acquired_percent`, you must use the final potential ownership percentage.

- **Data Hygiene:** If a field's value is not mentioned in the text, its `value` must be `null`. When extracting numbers, you must remove formatting like commas or currency symbols (e.g., "$1,500,000" becomes `1500000`).
</edge_cases_and_learnings>