# ProcureGraph: technical architecture

This describes how the MVP is actually built: the stack, the data model, what each pipeline
stage computes, and where the implementation departs from the system design document.
It assumes you have read the design document, or at least know that S1 to S5 are detectors,
S6 is the context layer and S7 is fusion.

Companion to [README.md](../README.md), which covers what the system does and how to run it.

## Stack

Nothing here needs a build step, a package manager for the frontend, or a network connection
at runtime.

| Layer | Choice | Version | Why this |
|---|---|---|---|
| Language | Python | 3.11.5 | The analysis is numeric and the whole team can read it |
| Dataframes | pandas | 3.0.5 | Group-bys and joins for the per-tender screens |
| Numerics | numpy | 2.4.6 | Percentiles, MAD, least squares for the price model |
| Storage | SQLite | 3.42 (stdlib `sqlite3`) | Single file, no server, ships with the repo |
| API | FastAPI | 0.141.1 | Typed query params and a free OpenAPI page at `/api/docs` |
| Server | uvicorn | 0.52.4 | ASGI, `--reload` during development |
| Frontend | Vanilla ES2020 + CSS custom properties | — | No build, no framework, no bundler |
| Charts | Hand-written inline SVG | — | See note below |

The chart decision is worth explaining. Chart.js or D3 from a CDN would have been quicker to
write, but the three visuals needed here (a contribution bar chart, a one-dimensional bid
scatter, a small node-link diagram) are about 120 lines of SVG string templating between them.
Hand-writing them removes a runtime network dependency, keeps the node-link layout under our
control, and lets the charts inherit the same CSS custom properties as everything else, so
there is one place to change a colour.

Total: roughly 2,400 lines of Python and 900 of frontend.

| File | Lines | Role |
|---|---|---|
| `scripts/generate_data.py` | 792 | Synthetic record generation |
| `scripts/load_sqlite.py` | 45 | CSV to SQLite |
| `scripts/analyze.py` | 1,414 | S1 to S7 |
| `app/main.py` | 165 | HTTP API |
| `app/static/app.js` | 495 | Views, rendering, SVG charts |
| `app/static/styles.css` | 268 | Tokens and layout |
| `app/static/index.html` | 139 | Shell for three views |

## Pipeline

Three scripts run in order. Each writes an artefact the next one reads, so any stage can be
re-run alone, and the intermediate CSVs are inspectable in a spreadsheet.

```
generate_data.py ──> data/raw/*.csv ──> load_sqlite.py ──> procuregraph.db
                                                                  │
                                                    analyze.py ───┤ reads source tables
                                                                  └─> writes 8 analysis tables
                                                                          │
                                                             app/main.py ─┘ read-only queries
```

### Stage 1: generation

Deterministic under `SEED = 20260917`, so tender IDs are stable across runs.

Buyers come first: 6 governments (5 states and the union government), each with a sample of
departments, each department with one or two district offices, 38 offices in total. Every
office carries a `region_factor` around 1.0 that later shifts its prices.

Vendors are drawn from 14 category profiles. Each gets an incorporation date, a size tier that
determines paid-up capital and turnover, a building and unit address, phone, email, bank
account, and one to three directors drawn from a shared person pool. Roughly 6% land at a
mass-registration building, 8% of the small firms share a filing consultant's phone, and 5%
route payments through one intermediary account. Those three values are the noise that the
rarity term and the suppression list have to cope with.

Prices are generated, not sampled. For each tender:

```
fair_rate  = base_rate[category] × price_index(date) × region_factor × U(0.94, 1.08)
fair_value = fair_rate × quantity
estimate   = fair_value × U(0.98, 1.12)
```

`price_index` is 5.5% a year compounding from 2022. Bids are then drawn according to the
scenario the tender was assigned:

| Scenario | Bid construction |
|---|---|
| competitive | every bidder at `fair_value × U(0.86, 1.14)`, lowest wins |
| cover | winner at `estimate × U(1.06, 1.18)`; losers clustered at `winner × U(1.14, 1.22)` within ±0.15–0.6% of each other |
| fixed_markup | losers at exactly +5%, +10%, +15%, +20% of the winner |
| rotation / allocation | designated winner slightly above fair value, others 3–16% higher |
| single | one bid near the estimate |

The cover-bidding construction is what produces a large relative distance and a tiny
coefficient of variation among losers, which is precisely what S2 screens for. Nothing about
the detection logic is fed back into the generator, and the analysis never reads the
`scenario_truth` column.

Structural patterns are seeded on top: five cover rings (each linked by a different attribute
type), three rotation groups with no shared attributes at all, two allocation groups, ten
shells, favouritism pairs, single-bid-heavy buyers, four threshold-splitting sequences, and
amendment growth on a share of contracts.

The legitimate-but-unusual cases matter as much as the patterns. Proprietary OEM servicing
awarded to one firm for five years, a declared flood emergency in one district window with
prices 38% higher, framework call-offs, MSE-reserved tenders, a declared SPV, and eight honest
young micro-enterprises. Without these, S6 has nothing to do and the false-positive control is
untested.

Data gaps are seeded deliberately: 10% of tenders publish no estimate, 5% publish only the
award with no bid records, and works categories carry no itemised schedule.

### Stage 2: load

`pandas.read_csv` then `to_sql` per file, plus seven indexes on the join and filter columns.
The database is rebuilt from scratch each run. 4.5 MB.

### Stage 3: analysis

`analyze.py` runs in the design document's execution order, not its numbering order: S1 and S6
build foundations, S2 to S5 score against them, S6 applies context, S7 fuses.

#### Shared primitives

Four functions do most of the work, and every detector calls them rather than rolling its own:

```python
ramp(v, start, full) = clamp((v - start) / (full - start), 0, 1)
noisy_or(pairs)      = 1 - Π(1 - w·e)
robust_z(x, peers)   = (x - median) / (1.4826 × MAD)     # falls back to σ when MAD = 0
smooth_rate(w, b)    = (w + 1) / (b + 5)                 # α = 1, β = 4
```

`percentile_of` computes the suspicion percentile `u` as the share of peers less extreme in the
suspicious direction, with a direction flag because low coefficient of variation is suspicious
while high relative distance is.

Signal families are enforced structurally: each detector collects signals into a dict keyed by
family, takes `max(w·e)` within each family, then applies noisy-OR across families. A family
belongs to exactly one detector, which is why a shared submission fingerprint sits in S1 and
not in S2.

#### S1: relationship graph

Attributes are inverted into buckets of `(kind, value) -> {vendor_ids}`. Buckets with fewer
than 2 or more than 60 vendors are dropped, which removes both the useless and the
placeholder-like values. Suppression-list values are excluded before the inversion.

For every pair sharing a bucket:

```
rarity(n) = max(floor_type, min(1, 3 / n))
eff       = w_type × rarity(n)
L(a, b)   = 1 - Π(1 - eff_i)      over the strongest instance of each attribute kind
```

| Attribute | w_type | floor |
|---|---|---|
| Bank account | 0.95 | 0.80 |
| Director or partner | 0.90 | 0.60 |
| Phone | 0.70 | 0.05 |
| Email (exact, non-webmail) | 0.70 | 0.05 |
| Address, unit level | 0.50 | 0.05 |
| Address, building level | 0.20 | 0.05 |

The high floors on bank accounts and directors and the low floors on infrastructure attributes
are what make this behave sensibly: an address shared by 40 firms decays to nearly nothing
while a bank account shared by 4 keeps most of its weight.

Clusters are connected components over links ≥ 0.5, via union-find. Only direct links between
two bidders in the same tender are scored; a path through a third party is visible in the graph
but never contributes.

Two signals fire: `S1.a` linked co-bidders, evidence `L_max` with ω = 1.0 when the winner is in
the linked pair and 0.8 otherwise; `S1.b` repetition, `ramp(k-1, 0, 5)` at weight 0.4 where k is
the number of tenders in which two members of the cluster met.

Confidence is the mean over bidders of 1.0 when at least two of {PAN, unit address, phone, bank
account, directors} are present and 0.5 otherwise, multiplied by 0.9 because this dataset has
no bid submission IP or device data.

Result: 196 scored links across 95 tenders.

#### S2: cover bidding

Screens are computed once for every tender up front, then compared against peer distributions.

```
RD  = (b₂ - b₁) / σ_L          needs n ≥ 3; σ_L floored at 0.1% of μ_L
CV_L = σ_L / μ_L
D   = (b₂ - b₁) / b₁            the only distribution screen when n = 2
```

`RD` and `CV_L` become evidence through `ramp(u, 0.90, 0.99)` against the peer group, so a
tender is judged tight or gapped relative to comparable procurement rather than against a fixed
number. Both sit in the `bid_distribution` family, so only the stronger one counts.

Behavioural signals use history rather than this tender's prices: perpetual losers
(`ramp(losses against this winner, 4, 10) × (1 - own win-rate percentile)`), submissions within
two minutes of each other, and repeated trivial disqualification in tenders won by the same
firm. The estimate-anchored pattern fires when the winner is within +15% of the estimate while
every loser exceeds +25%.

Applicability is explicit. Fewer than two valid price bids gives `not_applicable`; no published
bid records gives `insufficient_data` with confidence 0. Neither is reported as a score of zero.

`c₂ = c_n × c_peer × c_quality`, where `c_n` is 0.4 / 0.7 / 1.0 for 2 / 3 / 4+ bids and
`c_quality` takes 0.85 penalties for a missing itemised schedule and a missing estimate.

#### S3: shell company

Scored per vendor across six families, then lifted to tender level as the highest-scoring
bidder, with a flag for whether that vendor won or lost. The distinction matters: a shell that
wins is money at risk, a shell that loses is staged competition, and the dossier says which.

The single-trait cap is the important part. If fewer than two families produce `w·x ≥ 0.15`,
the score is held at 0.35. Being young, small and thinly capitalised describes thousands of
honest firms, so no one trait can raise a serious alert on its own. MSE or start-up
registration halves the age weight from 0.5 to 0.25. Government-owned suppliers are
`not_applicable` rather than scored.

`c₃` is the share of the six families that had any data.

#### S4: price and contract value

Module 4A fits one log-linear model per category:

```
ln(award / quantity) = β₀ + β₁·ln(quantity) + β₂·region_factor + β₃·(year - 2022) + r
```

by `numpy.linalg.lstsq`, needing at least 12 observations. The residual `r` is then compared to
the residuals of the tender's own peer group by robust z, and turned into evidence with
`ramp(z, 3, 6)` at weight 0.8 for overpricing and `ramp(-z, 3, 6)` at weight 0.3 for
abnormally low prices. Logs are used because doubling and halving a price should be equally
large deviations.

4B covers award over estimate, the estimate leak band (winner within ±0.5% of the confidential
estimate, repeatedly, for one buyer-vendor pair) and the inflated estimate signal, which is the
same model applied to the estimate itself and which points at the buyer rather than the vendor.

4C compares `final / original` contract value against the category's 90th percentile of growth,
floored at 1.10, and multiplies evidence by 1.2 when the first amendment lands inside the first
fifth of the contract period.

4D looks for the same buyer, same S1 cluster and same category placing several contracts within
60 days, each sized in [0.8T, T) against the threshold in force on that date, totalling at or
above T. Thresholds are time-versioned in `thresholds.csv`, so a contract is judged against the
rule that applied when it was signed.

`c₄ = c_peer × c_granularity`, where granularity is 1.0 with line items, 0.7 with a scope
metric, 0.4 with a total only.

#### S5: concentration, rotation and competition health

5A compares a vendor's win rate with one buyer against its win rate everywhere else, both Beta
smoothed, and takes a conservative lift: the lower bound of one over the upper bound of the
other. `e = clamp(log₂(lift) / 3, 0, 1)` at weight 0.7. Using lift rather than raw share is what
stops a genuinely cheap vendor that wins everywhere from triggering it.

5B gates before it scores. A candidate group is an S1 cluster that co-bid at least five times,
or a set of frequent co-bidders. It must meet in at least five tenders, have at least two
distinct winners, and split wins evenly (normalised entropy ≥ 0.8). Only then is evidence
computed, by counting how often the same member won two consecutive group tenders and comparing
that against 1,000 shuffles of the winner order: `e = ramp(-log₁₀ p, 1.3, 3)`. Confidence is
multiplied by 0.8 when the group was assembled from co-bidding alone with no S1 link behind it.

5C measures, per member, the share of its wins in its top district minus the share of its bids
there, averaged over the group. Honest firms win roughly where they bid.

5D scores the buyer, not the tender: single-bid rate, share of limited and direct procedures,
and share of tenders closing within a week, each as a percentile against the other 37 buyers.
Buyer-level signals attach to every tender of that buyer.

`c₅ = ramp(tenders in the buyer's history, 5, 20)`.

#### S6: peer groups and context

Peer selection walks a five-step ladder, stopping at the first level holding 30 or more
comparable tenders:

| Level | Definition | Tenders landing here | Mean peers |
|---|---|---|---|
| 0 | category + state + size band + ±24 months | 41 | 34 |
| 1 | size band dropped | 88 | 33 |
| 2 | window widened to ±48 months | 148 | 38 |
| 3 | region widened to national | 709 | 95 |
| 4 | all periods, all regions | 17 | 16 |

`c_peer = 0.85^level`, so a widely borrowed baseline produces a weaker alert rather than a
louder one, and the level is carried into the dossier so an investigator can see that a
comparison rests on national rather than district data. Below 10 peers at the widest level the
detector returns `insufficient_data`.

Context rules run after scoring, not before. Each documented fact names the detectors it may
dampen and the factor `m` it applies:

| Fact | Dampens | m |
|---|---|---|
| Framework call-off | S4, S5 | 0.20 |
| Proprietary or OEM-authorised item | S4, S5 | 0.30 |
| Declared emergency | S4, S5 | 0.40 |
| Declared SPV | S3 | 0.40 |
| Approved scope change | S4 | 0.50 |
| Reserved procurement | S5 | 0.50 |

When several rules hit one detector the strongest dampening wins and every rule that fired is
named, because the dossier has to show all of them. Nothing is suppressed to zero, and S1 is
floored at 0.7 regardless — a document explaining a relationship may itself be the thing that
was arranged.

Current counts: 6 framework call-offs, 13 reserved tenders, 17 approved scope changes, 5 SPV
awards, 4 proprietary, 4 emergency.

#### S7: fusion

```
x_k = w_k · c_k · m_k · S_k
R   = 1 - Π(1 - x_k)
I   = clamp(log₁₀(value at risk in lakh) / 4, 0.1, 1)
P   = R × (0.5 + 0.5·I)
```

Weights are 0.90 / 0.75 / 0.60 / 0.60 / 0.50 for S1 to S5, chosen by judgement and labelled as
such. Confidence multiplies into the score, so a dramatic signal resting on thin data cannot
outrank a solid one.

The corroboration cap holds `R` at 0.55 when at most one detector reaches `x ≥ 0.25`. It fires
on 103 of 1,003 tenders. Without it, one loud detector alone would reach the top tier, which
is a case worth queuing rather than one worth dropping everything for.

Contribution shares fall out of the arithmetic rather than a separate explanation model:

```
contribution_k = -ln(1 - x_k) / Σ -ln(1 - x_j)
c_case         = Σ contribution_k · c_k
```

This is exact and sums to 100%, which is why the dossier can state a detector's share as a
percentage without qualification.

Cases group tenders sharing a cluster and a buyer, capped at 50 members. Impact is then
measured on the case total, not the single contract: a tender belonging to an eleven-contract
scheme carries the scheme's value at risk. Tiers are assigned from `P` with a confidence gate of
0.6 for Tier 1, and a separate data-gap route for `P ≥ 0.50` with confidence below 0.4, so a
broken pipeline is sent to data owners rather than to investigators.

| Tier | n | P range | Mean confidence |
|---|---|---|---|
| Tier 1 — investigate now | 55 | 0.76–0.84 | 0.89 |
| Tier 2 — queue for review | 53 | 0.50–0.75 | 0.94 |
| Tier 3 — monitor | 461 | 0.30–0.50 | 0.97 |
| Tier 4 — no action | 434 | 0.00–0.30 | 0.94 |

## Data model

Eleven source tables carry the records. Eight analysis tables carry the results, all keyed by
`tender_id` so the API can assemble a dossier with simple lookups.

| Table | Rows | Contents |
|---|---|---|
| `tender_analysis` | 1,003 | One row per tender: R, P, tier, confidence, value at risk, peer level, case id, headline, rules version |
| `detector_results` | 5,015 | Five rows per tender: status, S, c, m, w, x, contribution % |
| `signals` | 4,293 | Every individual signal with observed value, baseline text, e, w, and a sentence of explanation |
| `vendor_links` | 196 | Scored bidder-to-bidder links with strength and the attribute behind them |
| `cases` / `case_tenders` | 421 / 1,003 | Case grouping and membership |
| `innocent_explanations` | 3,148 | Per-tender checkable alternatives, drawn from each contributing detector |
| `data_quality` | 2,891 | What was missing and what could not be checked |

Storing the explanation text at analysis time rather than generating it in the API means every
sentence an investigator reads was produced by the same code that produced the number, and both
carry the same `rules_version` stamp.

## API

Six read-only endpoints. No writes, no auth — this is a local prototype, and the design
document is explicit that a real deployment restricts dossiers to authorised investigators.

| Endpoint | Purpose |
|---|---|
| `GET /api/filters` | Governments, departments, offices, categories, methods for the filter panel |
| `GET /api/overview` | Tier counts, detector coverage, department rollup, top 20 by priority |
| `GET /api/search` | Filter by government, department, office, category, method, tier, minimum priority, free text; sort and limit |
| `GET /api/tender/{id}` | The full dossier: analysis, detectors with nested signals, bids, links, timeline, context, innocent explanations, data quality, case members |
| `GET /api/vendor/{id}` | Registration facts, directors, award and bid history |
| `GET /api/docs` | OpenAPI, free with FastAPI |

`/api/tender/{id}` assembles the timeline server side by merging tender publication, each bid,
the award, contract signature, every amendment and each bidder's incorporation date into one
sorted list. That is what makes the "registered, then bid, then won, then struck off" sequence
legible at a glance.

## Frontend

One HTML file holding three sections, toggled by an `active` class. No router, no framework,
no build. Rendering functions return template strings; there is no virtual DOM and no
reconciliation, because every view is replaced wholesale on navigation and the largest table is
150 rows.

Theming runs entirely through CSS custom properties on `:root` — a dark slate base with tier
colours (red, amber, blue, slate, violet) and a per-detector colour that stays consistent
between the contribution chart, the detector headers and the coverage bars. Numbers use
`Fira Code` with tabular figures so columns line up and values do not jitter as they change.

Accessibility work that is actually in the file: visible focus rings retained throughout,
keyboard handlers on the table rows and tiles that act as buttons, `role="img"` with
`aria-label` on each chart, `<title>` elements inside SVG nodes for pointer inspection, and a
`prefers-reduced-motion` block that collapses every animation. Layout collapses to one column
below 1100px and to a stacked header below 640px.

The three charts:

- **Contribution bars** scale to the largest share rather than to 100%, so a case led by one
  detector still reads clearly, with an `<animate>` on width for the initial draw.
- **Bid scatter** places every bid on a shared value axis with the estimate as a dashed
  reference line, alternating dots above and below the axis to stop overlap when bids cluster —
  which is exactly what happens in the cover-bidding cases the chart most needs to show.
- **Relationship map** lays bidders on an ellipse, draws scored links as solid red lines and
  weak ones dashed, and labels every edge with its strength in a pill.

## Performance

Measured on the current dataset, Python 3.11 on Windows.

| Stage | Time |
|---|---|
| `generate_data.py` | 0.2 s |
| `load_sqlite.py` | 1.2 s |
| `analyze.py` | 19 s |
| API query, overview rollup | 0.45 ms |
| API query, filtered search | 0.30 ms |
| API query, dossier signals | 0.09 ms |

`analyze.py` started at 106 s. Three things accounted for most of that, all of the same shape:
DataFrame work inside per-row loops. The S1 confidence calculation rebuilt the full persons
table once per bidder, S3 scanned every tender per vendor to count awards, and three separate
loops ran a boolean mask over `tender_items` per tender. Replacing them with sets and dicts
built once brought it to 19 s with byte-identical scores. What remains is dominated by
`peer_index`, which runs fresh pandas filters per tender — 1,003 times over the full frame.
Precomputing peer groups per (category, state, size band) bucket would take another large bite
if the dataset grew.

Query latency is sub-millisecond because the analysis is precomputed. The API only reads rows;
no scoring happens at request time.

## Where this departs from the design document

Stated plainly, because a doc that claims full coverage would be wrong.

**Not implemented at all.** Entity resolution tiers 2 and 3 — the generator emits clean PANs so
name-variant matching has nothing to do. Louvain re-splitting of clusters above 25 vendors, and
shared-service-provider node detection; the suppression list plus the 60-vendor bucket cap
handles the same problem here. Leave-one-out bid screens. Identical line-item comparison (S2.f),
which needs per-bidder price schedules the generator does not emit. S1.c buyer-side links, since
there is no official data. S4's buyer-level threshold bunching (4D.2). S5's HHI signal and
cancel-and-reissue. The investigator feedback loop, the stateful alert budget, and baseline
version stamping.

**Implemented differently.**

- The 5A Beta bounds use a normal approximation to the posterior rather than exact Beta
  quantiles, to avoid a scipy dependency. Conservative enough for triage and easier to explain,
  but it is an approximation.
- S2.e fixed-ratio markups looks at round percentages within one tender. The document specifies
  repeats across three or more tenders of the same group, which is the stronger test.
- The 4A price model is fitted per category across the whole dataset, then residuals are
  compared within the peer group. The document fits within the peer group itself. Fitting
  globally is more stable at this data volume but means a captured category could drag its own
  baseline — the poisoned-baseline risk the document names.
- Case assembly groups by (cluster, buyer) rather than seed-and-grow with radius 2 and Jaccard
  merging. Same outcome on this data, less machinery.
- 5B evidence 2 uses a fixed ramp on outsider win share rather than a percentile against the
  market norm.
- S1 has no time-overlap discount. Dates are carried on every record and the factor is specified
  at 0.3 beyond two years, but the link strength calculation does not yet apply it, so a recycled
  phone number would not be discounted.
- S3 counts the address, footprint and life-cycle families as "has data" unconditionally, which
  makes `c₃` optimistic. A stricter reading would only count a family when its underlying fields
  are populated.

**Present but untested by the data.** The `not_applicable` path for government-owned suppliers,
the duplicate-bidder signal (S1.d), and the two-bid gap screen all work, but the generator
produces few or no cases that exercise them hard.

## Reproducing and extending

```bash
python scripts/generate_data.py   # regenerate CSVs, deterministic
python scripts/load_sqlite.py     # rebuild the database
python scripts/analyze.py         # rescore, prints tier distribution and truth table
python -m uvicorn app.main:app --reload --port 8077
```

The three parameter groups worth touching first, in the order the design document recommends
tuning them: the ramp bounds inside each detector, then the within-detector weights, then the
fusion weights `WK` in `analyze.py`. Ramps should be set from peer percentiles rather than
judgement; the current values are the document's starting values.

To test whether a change helps, compare the mean priority by `scenario_truth` that `analyze.py`
prints at the end. The generator plants the pattern, the analysis never reads the column, and
the gap between planted and ordinary tenders is the signal. On the current run cover bidding
sits at 0.76 against 0.33 for ordinary competition, while rotation reaches only 0.34 — those
patterns need longer histories than three years of a thousand tenders can provide, which is why
the document lists 5B and 5C as the first modules to cut.
