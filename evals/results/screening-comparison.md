# Screening comparison on 43 labelled announcements

| | Calls | Deal or not, correct | Continent, correct | Median latency | Tokens in / out | Cost per 1,000 |
|---|---|---|---|---|---|---|
| Two chat calls (`gpt-6-luna`) | 2 | 41 of 43 | 40 of 40 | 5.98 s | 1688 / 243 | $0.29 |
| One Decisions API call (`gpt-6-luna`) | 1 | 38 of 43 | 39 of 40 | 0.61 s | 1229 / 0 | $0.12 |
| One Jev call (`typesafe/jev-1.14`, OpenRouter) | 1 | 38 of 43 | 40 of 40 | 0.59 s | 1549 / 0 | $0.07 |

Labels: 40 project deals, 3 not.
Chat and Decisions API cost uses $0.10 / $0.50 per million input / output tokens; Jev cost is the `usage.cost` OpenRouter reports (input tokens only; output is free). Jev's examples and counterexamples are in its criteria text (JEV_DEAL in this file); it has no few-shot field.

## Cases where a method was wrong or the methods disagreed

- `16379455` Vault Minerals Shareholders Approve Genesis Merger: label deal=False, None; chat deal=False, Oceania; decisions deal=False (p=0.02), Oceania; jev deal=True (p=0.63), Oceania
- `16402843` Arrow Secures $5.5M To Fund Pannawonica Acquisition: label deal=True, Oceania; chat deal=True, Oceania; decisions deal=False (p=0.05), None; jev deal=True (p=0.81), Oceania
- `16433093` American Eagle Secures C$46,265 Top-Up Investment From South32 to Main: label deal=False, None; chat deal=False, None; decisions deal=False (p=0.00), North America; jev deal=False (p=0.06), None
- `65874` Sorrento Resources Expands Copper-Gold Lord Baron Project, Newfoundlan: label deal=True, North America; chat deal=True, North America; decisions deal=False (p=0.01), North America; jev deal=False (p=0.09), North America
- `30094` WestKam Gold Corp. Acquires BC Mineral Claim in Goldbridge Mining Camp: label deal=True, North America; chat deal=True, North America; decisions deal=True (p=0.62), North America; jev deal=False (p=0.10), North America
- `91669` Geologica Stakes High Potential Lincoln Placer Claims: label deal=True, North America; chat deal=True, North America; decisions deal=False (p=0.00), North America; jev deal=False (p=0.06), North America
- `64139` Dore Copper Enters Into an Agreement to Acquire Claims Next to Its Fla: label deal=True, North America; chat deal=False, North America; decisions deal=False (p=0.00), North America; jev deal=False (p=0.37), North America
- `87563` Kodiak Confirms Potential of MPD Northwest Claims and Adds Further Str: label deal=True, North America; chat deal=False, North America; decisions deal=False (p=0.00), North America; jev deal=True (p=0.70), North America

## Headline filter: words against Jev on the headline alone

| Headlines | Word filter passes | Jev passes (p >= 0.5) | Jev passes (p >= 0.2) |
|---|---|---|---|
| Labelled deals (40) | 35 | 39 | 39 |
| Labelled non-deals (3) | 3 | 1 | 1 |
