# DC 311 — Submission Methods & Rates

**Interactive dashboard:** [`dashboard.html`](dashboard.html) — open in a browser
(self-contained, no server needed). Raw tables: `export_*.csv` (drop into Excel).

Rates generated 2026-06-16; methods refreshed 2026-11-20 (data through 2026-11-18). Two data sources:

| What | Source | Rows | Exact? |
|------|--------|------|--------|
| Submission **rates** (volume over time) | full bulk ArcGIS dataset 2009–2026 | 4,966,653 | exact |
| Submission **methods** (`source`/`origin`) | complete 2026 capture of API-served service types (`data/methods.json`) | 242,871 | exact for covered types |

The bulk dataset still has **no** source/origin field. Method figures come from a
per-SR lookup covering **every** SR of the API-served service types, so they are no
longer a sample. Shares describe API-served types only, not all of 311.

## How requests are submitted (2026, n=242,871, API-served types)

Grouping the raw `source` field into channel families:

| Channel | Count | Share | Raw sources included |
|---------|------:|------:|----------------------|
| **Phone / call center** | 95,077 | **39.1%** | `Agent`, `AWS Connect - Production`, `IVR` |
| **Mobile web** | 60,400 | **24.9%** | `iOS Browser`, `Android Browser`, `Mobile` |
| **Native mobile app** | 28,305 | **11.7%** | `iOS`, `Android` |
| **Desktop web** | 56,409 | **23.2%** | `Web` |
| **Other** | 2,680 | **1.1%** | `snap311`, `DPW-Trakster Key`, `DowntownBid`, `JPtest`, `API` |

Mobile web + native = 88,705 (36.5%), the coarse "Mobile (app + web)" bucket.

Raw breakdown: Agent 36.8% · Web 23.2% · iOS Browser 20.9% · iOS 9.7% ·
Android Browser 3.9% · AWS Connect - Production 2.3% · Android 2.0% · snap311 1.1% ·
other (6 sources) 0.1%.

**Takeaways for Snap311:**
- **~60% of submissions are already digital** (mobile web + desktop web + native app;
  59.7%). Phone is still the single biggest channel (39.1% vs 36.5% for all mobile)
  but is a minority.
- **Mobile web (24.9%) outnumbers the native app (11.7%) by ~2.1×.** People reach for
  the browser on their phone more than DC's native app. That's the opening Snap311
  targets — the demand for mobile self-service exists; the native experience is just bad.
- Within mobile, **iOS leads Android ~5:1** on both web (5.3:1) and native (4.9:1) —
  prioritize iOS.

## Origin (system of record)

API 63.1% · Phone 33.2% · Email 2.8% · Live Agent 0.4% · Twitter 0.4% · other (1 source) 0.0%.
"API" = anything that came through the digital intake (web/app), confirming the
digital-majority story above (digital channels total 59.7%).

## Submission rates (volume, full dataset)

Yearly request volume:

```
2009   21,617      2015  298,413      2021  360,810
2010   30,199      2016  304,448      2022  386,691
2011   10,550      2017  311,411      2023  425,213
2012  260,608      2018  338,793      2024  422,282
2013  235,024      2019  367,016      2025  440,562
2014  325,538      2020  305,529      2026  457,347   <- partial (thru mid-Nov)
```

- System matured 2012 (jump from ~10k to ~260k once intake was digitized).
- Steady growth since, apart from the 2020 COVID dip: ~305k (2016) → 425k (2023) →
  422k (2024, flat) → 441k (2025). 2026 already passed 2025 by mid-November, so it is
  a **record year**.
- 2016+ totals match DC's live per-year layer counts to within 0.05%. An earlier
  version of this table showed a "slight softening 2024–25" (398k / 356k). That was a
  pipeline bug, not a real trend: those two years were frozen at mid-year snapshots
  (Dec 4, 2024 and Oct 6, 2025) and never completed. Fixed in DCD16.
- See the monthly-2026 chart for within-year cadence.

## Channel mix over time (2026, weekly)

The headline question from the handoff — *did native-app submissions step-change on
2026-05-01?* — is answerable at full coverage. Weekly totals are now ~4,000–5,600 SRs
(ISO weeks, UTC). W18 straddles May 1 and is excluded from the comparison.

| Week (Mon) | SRs | Native app n | Native share |
|------------|----:|-------------:|-------------:|
| W14 (Mar 30) | 3,904 | 300 | 7.7% |
| W15 (Apr 6) | 4,445 | 312 | 7.0% |
| W16 (Apr 13) | 4,794 | 357 | 7.4% |
| W17 (Apr 20) | 4,735 | 359 | 7.6% |
| W18 (Apr 27) (straddles) | 4,538 | 357 | 7.9% |
| W19 (May 4) | 5,454 | 424 | 7.8% |
| W20 (May 11) | 5,461 | 365 | 6.7% |
| W21 (May 18) | 5,399 | 389 | 7.2% |
| W22 (May 25) | 5,616 | 419 | 7.5% |

Pre (W14–W17) mean native share 7.4%; post (W19–W22) 7.3%; difference −0.2 pp. Post
weekly shares (6.7–7.8%) overlap the pre range (7.0–7.7%), so there is **no step change
visible at full coverage**. (Weekly SR totals also step up after May 1, when Rodents and Illegal Dumping
begin appearing in the API; native share is unaffected.)

## Method × service type

See the stacked "Method by service type" chart. Pattern matches the handoff hypothesis:
phone (`Agent`) over-indexes on trash/bulk/supercan categories (older callers), while
browser/web over-index on walk-around categories. Exact crosstab in
`export_source_by_service.csv`.

**Types missing from this chart and crosstab have no data, not low volume.** DC's
live API is the only source of `source`/`origin`. It serves some service types in
full and others not at all. Parking Enforcement, Residential Parking Permit, ROSA,
DMV, DC Government Information, Scheduled Yard Waste, Sanitation Enforcement and
the snow programs (about half of all requests) are absent in every year checked
(2024–2026). Rodents and Illegal Dumping appear only from 2026-05-01. See the README
section "Why the API has gaps".

## Files

- `dashboard.html` — interactive charts (open in browser)
- `agg.json` — all aggregated data
- `export_by_year.csv`, `export_by_month.csv` — volume/rates
- `export_source_counts.csv`, `export_channel_counts.csv`, `export_origin_counts.csv` — methods
- `export_source_by_service.csv` — method × service crosstab
- `scripts/aggregate_submissions.py`, `scripts/build_dashboard.py` — regenerate everything

## Caveats

- Method % cover every 2026 SR of the API-served service types (n=242,871 through
  2026-11-18). Types not served by the API are absent (whole types such as parking, DMV,
  info calls, yard waste and snow, about half of all requests), so shares describe
  API-served types, not all of 311. See README "Why the API has gaps".
- Times in the by-hour chart are **UTC** — subtract 4h for EDT before drawing
  push-notification conclusions.
- 2026 volume is partial. 2025 source/origin now resolves in the live API (checked
  2026-11-18), and 2025 has a method breakdown like other years.
