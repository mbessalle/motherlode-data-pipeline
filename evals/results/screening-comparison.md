# Screening comparison on 43 labelled announcements

| | Calls | Deal or not, correct | Continent, correct | Median latency | Tokens in / out | Cost per 1,000 |
|---|---|---|---|---|---|---|
| Two chat calls (`gpt-6-luna`) | 2 | 41 of 43 | 40 of 40 | 3.89 s | 1592 / 195 | $0.26 |
| One Decisions API call (`gpt-6-luna`) | 1 | 38 of 43 | 39 of 40 | 0.61 s | 1229 / 0 | $0.12 |
| One Jev call (`typesafe/jev-1.14`, OpenRouter) | 1 | 38 of 43 | 40 of 40 | 0.59 s | 1549 / 0 | $0.07 |

Labels: 40 project deals, 3 not.
Chat and Decisions API cost uses $0.10 / $0.50 per million input / output tokens; Jev cost is the `usage.cost` OpenRouter reports (input tokens only; output is free). Jev's examples and counterexamples are in its criteria text (JEV_DEAL in this file); it has no few-shot field.

## Cases where a method was wrong or the methods disagreed

- `16379455` Vault Minerals Shareholders Approve Genesis Merger: label deal=False, None; chat deal=False, Oceania; decisions deal=False (p=0.02), Oceania; jev deal=True (p=0.63), Oceania
- `16402843` Arrow Secures $5.5M To Fund Pannawonica Acquisition: label deal=True, Oceania; chat deal=True, Oceania; decisions deal=False (p=0.05), None; jev deal=True (p=0.81), Oceania
- `16433093` American Eagle Secures C$46,265 Top-Up Investment From South32 to Main: label deal=False, None; chat deal=False, North America; decisions deal=False (p=0.00), North America; jev deal=False (p=0.06), None
- `65874` Sorrento Resources Expands Copper-Gold Lord Baron Project, Newfoundlan: label deal=True, North America; chat deal=True, North America; decisions deal=False (p=0.01), North America; jev deal=False (p=0.09), North America
- `30094` WestKam Gold Corp. Acquires BC Mineral Claim in Goldbridge Mining Camp: label deal=True, North America; chat deal=True, North America; decisions deal=True (p=0.62), North America; jev deal=False (p=0.10), North America
- `91669` Geologica Stakes High Potential Lincoln Placer Claims: label deal=True, North America; chat deal=True, North America; decisions deal=False (p=0.00), North America; jev deal=False (p=0.06), North America
- `64139` Dore Copper Enters Into an Agreement to Acquire Claims Next to Its Fla: label deal=True, North America; chat deal=False, North America; decisions deal=False (p=0.00), North America; jev deal=False (p=0.37), North America
- `87563` Kodiak Confirms Potential of MPD Northwest Claims and Adds Further Str: label deal=True, North America; chat deal=False, North America; decisions deal=False (p=0.00), North America; jev deal=True (p=0.70), North America

## Headline filter: words against Jev on the headline alone

| Headlines | Word filter passes | Jev passes (p >= 0.5) | Jev passes (p >= 0.2) |
|---|---|---|---|
| Labelled deals (40) | 29 | 39 | 39 |
| Labelled non-deals (3) | 3 | 1 | 1 |
| Analyst's deal table (247) | 160 | 221 | 238 |

Analyst headlines that a filter drops (word filter, Jev p):

- WYX Secures Prospective Gallium-Bauxite Project in WA (dropped, 0.95)
- Agreement Executed with GDM for Adelong Project (dropped, 0.93)
- Novo Reaches Second and Final Completion Milestone With Creasy Group (dropped, 0.79)
- FG1 Secures Strategic EL Application at Beaconsfield NE Tas (dropped, 0.45)
- New tenements expand Kanowna Gold Project (dropped, 0.97)
- Katanning Gold Project Mining Leases Granted (dropped, 0.89)
- Lady Ida Completes and Appointment of New Director (dropped, 0.07)
- Matsa and AngloGold Execute $101M Deal for Lake Carey (dropped, 0.98)
- Catalina extends presence at Laverton (dropped, 0.63)
- Agreement with Mark Creasy over Peninsula Prospect - Amended (dropped, 0.96)
- Queensland EMP Application at Triumph Project (dropped, 0.36)
- KNB expands Enmore Project, securing gold-antimony targets (dropped, 0.34)
- Trading Halt (dropped, 0.16)
- ADG: Completion of Cosmo Gold Transaction (dropped, 0.96)
- Munda Agreement with Auric Mining Ltd concluded (dropped, 0.89)
- Sale of shares in Cobra Resources (passes, 0.47)
- Execution of Li Partnership with Japanese Government JOGMEC (dropped, 0.77)
- GIB: Acq'n of New Mining Lease at Edjudina Gold Project WA (dropped, 0.98)
- John Bull Gold Update (dropped, 0.17)
- Presentation - Murchison Gold Strategy (dropped, 0.19)
- Forrestania Growth Strategy Commences (dropped, 0.30)
- Leviathan Gold granted new Exploration License over the former Queen's Birthday Mine in Vi (dropped, 0.96)
- Dart Moves on Raglan Goldfield (dropped, 0.81)
- Randalls Tenement Applications Expand Land Position (dropped, 0.96)
- NVO: Strengthens Portfolio w/ Two High-Grade Gold Projects (dropped, 0.88)
- Novo Secures Strategic Land Position in the Onslow District Western Australia (dropped, 0.91)
- Yardilla New Gold Project (dropped, 0.31)
- OUTBACK GRANTED SILVER SPOON EXPLORATION LICENCE NEAR FOSTERVILLE GOLD MINE (dropped, 0.88)
- New Tenement Grant - Leake Prospect (dropped, 0.95)
- Agreement Over Advanced Antimony Gold Project - Amendment (dropped, 0.97)
- Amendment to Announcement made on 5 February 2025 (dropped, 0.43)
- Accelerated Payment of Deferred Consideration (dropped, 0.71)
- Completion of Eagle Downs transactions (dropped, 0.96)
- Solstice Secures Strategic Copper Exposure (dropped, 0.80)
- Horseshoe Lights Project Commercial Development (dropped, 0.33)
- Company Update and Due Diligence (dropped, 0.35)
- Infinity Secures Bogong Copper Gold Tenement in NSW (dropped, 0.97)
- Mutooroo Agreement With JX Advanced Metals (dropped, 0.86)
- Completion of Tenement Addition and Drilling Update (dropped, 0.90)
- New tenement application over Whim Creek greenstone belt (dropped, 0.92)
