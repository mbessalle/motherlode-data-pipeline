# Eval: `gpt-6.1-terra` on 33 press releases

## First attempt

| Field | Written in article | Derived by a rule | Unsupported | Empty | Supported |
|---|---|---|---|---|---|
| `cash_payments_value` | 11 | 7 | 0 | 15 | 100% |
| `exp_comit_value` | 7 | 4 | 0 | 22 | 100% |
| `shares_amount` | 12 | 7 | 1 | 13 | 95% |
| `share_price` | 4 | 0 | 0 | 29 | 100% |
| `shares_value` | 3 | 1 | 1 | 28 | 80% |
| `aggregate_deal_value` | 1 | 7 | 1 | 24 | 89% |
| `nsr_percent` | 15 | 0 | 1 | 17 | 94% |
| `interest_acquired_percent` | 21 | 0 | 2 | 10 | 91% |
| `project_area` | 20 | 0 | 0 | 13 | 100% |
| `drilling_amount` | 0 | 0 | 0 | 33 | - |

- Values supported by the article: **120 of 126 (95%)**
- Records with every value supported: **30 of 33**

## Agreement with the analyst's spreadsheet

| Field | Both filled | Agree | Model only | Analyst only |
|---|---|---|---|---|
| `cash_payments_value` | 17 | 10 | 1 | 7 |
| `exp_comit_value` | 11 | 9 | 0 | 3 |
| `shares_amount` | 18 | 11 | 2 | 3 |
| `share_price` | 3 | 3 | 1 | 15 |
| `nsr_percent` | 16 | 13 | 0 | 9 |
| `interest_acquired_percent` | 23 | 22 | 0 | 9 |
| `project_area` | 20 | 14 | 0 | 7 |
