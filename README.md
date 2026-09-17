# ProcureGraph — MVP

An auditing prototype that surfaces public procurement activity and relationships deserving
human investigation. This repository implements subsystems S1 to S7 of the system design
document over synthetic, OCDS-like procurement records, and serves the results through a
web console.

All data here is invented. No real vendor, buyer, official or contract appears anywhere.

For the stack, data model, algorithms as implemented and the list of deviations from the
design document, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## What runs

```
scripts/generate_data.py   ->  data/raw/*.csv        synthetic OCDS-like records
scripts/load_sqlite.py     ->  data/procuregraph.db  CSV into SQLite
scripts/analyze.py         ->  same db, +8 tables    S1-S7 in pandas/numpy
app/main.py                ->  FastAPI + static UI   the investigator console
```

## Setup

Needs Python 3.11+ with `pandas`, `numpy`, `fastapi`, `uvicorn`.

```bash
pip install pandas numpy fastapi uvicorn
python scripts/generate_data.py
python scripts/load_sqlite.py
python scripts/analyze.py
python -m uvicorn app.main:app --reload --port 8077
```

Then open http://127.0.0.1:8077

Re-running `generate_data.py` is deterministic (fixed seed), so the same tender IDs come
back every time.

## The data

`data/raw/` holds eleven CSVs. The OCDS core (tender, bid, award, contract, amendment,
party) is extended with the fields the detectors actually need and which plain OCDS does
not carry: bank accounts, directors, incorporation dates, paid-up capital, address-sharing
counts, registry status, time-versioned approval thresholds, and the documented
justifications S6 reads (proprietary certificates, emergency declarations, framework
references, MSE reservations).

| File | Rows | What it holds |
|---|---|---|
| `buyers.csv` | 38 | Buying offices, with government level, department, district |
| `vendors.csv` | 259 | Firms, with registration, capital, contact and address facts |
| `vendor_persons.csv` | ~450 | Directors and partners, which is what makes shared control visible |
| `tenders.csv` | ~1000 | Tenders with estimate, quantity, method, and any recorded justification |
| `bids.csv` | ~3500 | Every bid, with submission time and disqualification reason |
| `awards.csv`, `contracts.csv`, `amendments.csv` | | Award, signed contract, and each amendment with its reason code |
| `tender_items.csv` | 218 | Line items where a price schedule exists, which raises S4 confidence |
| `thresholds.csv` | 4 | Approval thresholds by date, used by module 4D |
| `suppression_list.csv` | 4 | Governed allow-list: payment intermediary, business centre, filing consultant |

### What is planted in it

Around 80% of the tenders are ordinary competition. Deliberately seeded into the rest:

- five cover-bidding rings, each linked by a different attribute (bank account, shared
  director, phone, address), with tight losing bids and perpetual losers
- three bid-rotation groups with no shared attributes at all, so only the behavioural
  screens in S5 can find them
- two market-allocation groups that bid everywhere and win only at home
- ten shell companies, eight bidding as cover and two winning outright then going dormant
- four buyer–vendor favouritism pairs and four single-bid-heavy buyers
- four threshold-splitting sequences sitting just under the approval limit
- amendment inflation on a share of contracts

And, equally important, legitimate activity that a naive detector would flag: proprietary
OEM servicing awarded to the same firm for five years running, a declared flood emergency
with prices 38% above normal, framework call-offs, MSE-reserved tenders, a declared SPV,
and eight honest young micro-enterprises that look shell-like on paper.

Data gaps are seeded too: missing estimates, tenders where only the award was published,
and tenders with no itemised schedule. These lower confidence rather than the score, and
route to the data-gap tier instead of the investigation queue.

## The analysis

`analyze.py` follows the design document's execution order: S1 and S6 build the
foundations, S2–S5 score against them, S6 applies context, S7 fuses and explains.

- **S1** builds the vendor graph. Effective link weight is `w_type × rarity(n)`, with
  `rarity(n) = max(floor, min(1, 3/n))` so an address shared by 40 firms fades while a bank
  account shared by two does not. Links combine by noisy-OR; clusters are connected
  components over links ≥ 0.5. Only links between bidders in the same tender are scored.
- **S2** computes relative distance, the losers' coefficient of variation, the two-bid gap,
  the estimate-anchored pattern, round-percentage markups, perpetual losers,
  near-simultaneous submissions and trivial disqualifications. Each screen is turned into a
  suspicion percentile against its peer group. Within a signal family only the strongest
  counts.
- **S3** scores one vendor at a time across six families, with the single-trait cap: fewer
  than two families above 0.15 holds the score at 0.35. MSE and start-up registration halves
  the age weight; government-owned suppliers are `not_applicable`.
- **S4** fits `ln(unit price) ~ ln(quantity) + region + year` per category, takes a robust
  z of the residual against peers, then adds award-over-estimate, estimate leak, inflated
  estimate, amendment growth against the peer 90th percentile, and threshold splitting.
- **S5** uses conservative lift with Beta-smoothed bounds for favouritism, a permutation
  test on winner order for rotation, a win-share-versus-bid-share gap for allocation, and
  buyer-level competition health against peer buyers.
- **S6** walks the backoff ladder (category + state + size band + 24 months, then widening
  one dimension at a time) until 30 peers are found, multiplying `c_peer` by 0.85 per step
  and returning `insufficient_data` below 10 peers. It then applies the context factors `m`,
  never below their floor, and never below 0.7 for relationship evidence.
- **S7** fuses `x_k = w_k · c_k · m_k · S_k` by noisy-OR, applies the corroboration cap,
  scales by impact, assigns a tier, and computes exact contribution shares from
  `−ln(1 − x_k)`.

Every score in the database carries its confidence, its context factor, the peer group it
was compared against, the backoff level, and the rules version.

### Does it work?

The generator records the pattern it planted in `scenario_truth`, which the analysis never
reads. Mean priority by planted scenario on the current run:

| Planted | n | mean P |
|---|---|---|
| fixed-ratio markups | 13 | 0.79 |
| cover bidding | 37 | 0.76 |
| rotation | 23 | 0.34 |
| ordinary competition | 801 | 0.33 |
| single bid | 111 | 0.22 |
| market allocation | 18 | 0.23 |

Cover bidding separates cleanly. Rotation and allocation barely separate at all, which is
the honest result: those patterns need long histories, and three years of a thousand tenders
is thin. The design document says as much, and says to drop 5B and 5C first if time runs
short.

## The console

Two ways in, as asked. Type a tender ID into the box in the header, or filter down by
government, department, buying office, category, method, tier and minimum priority.

The queue view shows tier counts as an alert budget, per-detector coverage (what could
actually be checked, so silence reads as coverage rather than as a clean bill of health),
flagged tenders by department, and the top 20 by priority.

The dossier for a tender shows the headline, the priority band, a contribution chart
computed from the fusion arithmetic, the bid distribution against the estimate, each
detector expanded into its individual signals with observed value and peer baseline side by
side, the relationship map with labelled edges, a timeline, every context rule that fired
with its raw and adjusted values, the innocent explanations to check, and what could not be
checked at all.

Charts are hand-written SVG, so the page has no runtime dependency beyond the font.

## Scope

This is the MVP. Not built: the investigator feedback loop (dispositions feeding back into
context rules and weight calibration), the alert-budget queue with state, Louvain re-splitting
of oversized clusters, leave-one-out bid screens, identical line-item comparison, and entity
resolution across name variants — the generator emits clean identifiers, so tiers 2 and 3 of
the resolution logic have nothing to do here.

`scenario_truth` is left in the database on purpose. It is the evaluation harness, and it is
the one column a real deployment would not have.
