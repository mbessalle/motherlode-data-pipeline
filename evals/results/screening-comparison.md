# Screening comparison on 43 labelled announcements

| | Calls | Deal or not, correct | Continent, correct | Median latency | Tokens in / out | Cost per 1,000 |
|---|---|---|---|---|---|---|
| Two chat calls (`gpt-6-luna`) | 2 | 41 of 43 | 40 of 40 | 3.89 s | 1592 / 195 | $0.26 |
| One Decisions API call (`gpt-6-luna`) | 1 | 38 of 43 | 39 of 40 | 0.61 s | 1229 / 0 | $0.12 |

Labels: 40 project deals, 3 not.
Cost uses $0.10 / $0.50 per million input / output tokens.

## Cases where a method was wrong or the two disagreed

- `16402843` Arrow Secures $5.5M To Fund Pannawonica Acquisition: label deal=True, Oceania; chat deal=True, Oceania; decisions deal=False (p=0.05), None
- `65874` Sorrento Resources Expands Copper-Gold Lord Baron Project, Newfoundlan: label deal=True, North America; chat deal=True, North America; decisions deal=False (p=0.01), North America
- `91669` Geologica Stakes High Potential Lincoln Placer Claims: label deal=True, North America; chat deal=True, North America; decisions deal=False (p=0.00), North America
- `64139` Dore Copper Enters Into an Agreement to Acquire Claims Next to Its Fla: label deal=True, North America; chat deal=False, North America; decisions deal=False (p=0.00), North America
- `87563` Kodiak Confirms Potential of MPD Northwest Claims and Adds Further Str: label deal=True, North America; chat deal=False, North America; decisions deal=False (p=0.00), North America
