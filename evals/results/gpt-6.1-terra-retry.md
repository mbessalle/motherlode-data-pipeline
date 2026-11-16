# Eval: `gpt-6.1-terra` (through OpenRouter) on 33 press releases

## First attempt

| Field | Written in article | Derived by a rule | Unsupported | Empty | Supported |
|---|---|---|---|---|---|
| `cash_payments_value` | 11 | 6 | 1 | 15 | 94% |
| `exp_comit_value` | 7 | 2 | 2 | 22 | 82% |
| `shares_amount` | 12 | 4 | 3 | 14 | 84% |
| `share_price` | 4 | 0 | 0 | 29 | 100% |
| `shares_value` | 3 | 1 | 1 | 28 | 80% |
| `aggregate_deal_value` | 1 | 8 | 0 | 24 | 100% |
| `nsr_percent` | 15 | 0 | 1 | 17 | 94% |
| `interest_acquired_percent` | 25 | 0 | 0 | 8 | 100% |
| `project_area` | 20 | 0 | 0 | 13 | 100% |
| `drilling_amount` | 0 | 0 | 0 | 33 | - |
| `shares_value_scraped` | 3 | 0 | 1 | 29 | 75% |
| `drilling_value` | 0 | 0 | 0 | 33 | - |
| `owner_marketcap` | 0 | 0 | 0 | 33 | - |

- Values supported by the article: **122 of 131 (93%)**
- Records with every value supported: **27 of 33**

## After the corrected retry

| Field | Written in article | Derived by a rule | Unsupported | Empty | Supported |
|---|---|---|---|---|---|
| `cash_payments_value` | 11 | 6 | 1 | 15 | 94% |
| `exp_comit_value` | 7 | 2 | 2 | 22 | 82% |
| `shares_amount` | 12 | 4 | 3 | 14 | 84% |
| `share_price` | 4 | 0 | 0 | 29 | 100% |
| `shares_value` | 3 | 1 | 1 | 28 | 80% |
| `aggregate_deal_value` | 1 | 8 | 0 | 24 | 100% |
| `nsr_percent` | 16 | 0 | 0 | 17 | 100% |
| `interest_acquired_percent` | 25 | 0 | 0 | 8 | 100% |
| `project_area` | 20 | 0 | 0 | 13 | 100% |
| `drilling_amount` | 0 | 0 | 0 | 33 | - |
| `shares_value_scraped` | 3 | 0 | 1 | 29 | 75% |
| `drilling_value` | 0 | 0 | 0 | 33 | - |
| `owner_marketcap` | 0 | 0 | 0 | 33 | - |

- Values supported by the article: **123 of 131 (94%)**
- Records with every value supported: **28 of 33**

- Records retried: 6; of those, now fully supported: 1

## Agreement with the analyst's spreadsheet

| Field | Both filled | Agree | Model only | Analyst only |
|---|---|---|---|---|
| `cash_payments_value` | 17 | 10 | 1 | 7 |
| `exp_comit_value` | 11 | 9 | 0 | 3 |
| `shares_amount` | 17 | 12 | 2 | 4 |
| `share_price` | 3 | 3 | 1 | 15 |
| `nsr_percent` | 16 | 14 | 0 | 9 |
| `interest_acquired_percent` | 25 | 24 | 0 | 7 |
| `project_area` | 20 | 14 | 0 | 7 |
