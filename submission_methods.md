# DC 311 — Submission Methods & Rates

**Interactive dashboard:** [`dashboard.html`](dashboard.html) — open in a browser
(self-contained, no server needed). Raw tables: `export_*.csv` (drop into Excel).

Generated 2026-06-16. Two data sources:

| What | Source | Rows | Exact? |
|------|--------|------|--------|
| Submission **rates** (volume over time) | full bulk ArcGIS dataset 2009–2026 | 4,966,653 | exact |
| Submission **methods** (`source`/`origin`) | enriched 2026 sample (~50/day, looked up via live API) | 5,862 | representative sample |

The bulk dataset has **no** source/origin field, so method analysis is necessarily
sample-based. We bumped the sample from 1,708 → 5,862 for this pass.

## How requests are submitted (2026 sample, n=5,862)

Grouping the raw `source` field into channel families:

| Channel | Share | Raw sources included |
|---------|------:|----------------------|
| **Phone / call center** | **39.7%** | `Agent`, `AWS Connect - Production` |
| **Mobile web** | **27.5%** | `iOS Browser`, `Android Browser` |
| **Desktop web** | **25.2%** | `Web` |
| **Native mobile app** | **7.5%** | `iOS`, `Android` |

Raw breakdown: Agent 38.4% · Web 25.2% · iOS Browser 23.3% · iOS 6.0% ·
Android Browser 4.3% · Android 1.6% · AWS Connect 1.3%.

**Takeaways for Snap311:**
- **~60% of submissions are already digital** (mobile web + desktop web + native app).
  Phone is still the single biggest channel but is now a minority.
- **Mobile web (27.5%) dwarfs the native app (7.5%) by ~3.7×.** People reach for the
  browser on their phone, not DC's native app. That's the opening Snap311 targets —
  the demand for mobile self-service exists; the native experience is just bad.
- Within mobile, **iOS leads Android ~4:1** on both web and native — prioritize iOS.

## Origin (system of record)

API 61.6% · Phone 35.2% · Email 2.1% · Live Agent 0.6% · Twitter 0.5%.
"API" = anything that came through the digital intake (web/app), confirming the
digital-majority story above.

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

Weekly channel-share is plotted but **noisy** at ~110 samples/week. The headline
question from the handoff — *did native-app submissions step-change on 2026-05-01?* —
is **not cleanly answerable even at n=5,862**; native app is only ~7.5% of the sample,
so weekly native counts are single digits. To call that step-change you'd need either
the full population's source field (not exposed) or a much larger targeted sample of
just the native-app SRs.

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

- Method % cover **only the request types DC's API serves**, about half of all
  requests. Whole types (parking, DMV, info calls, yard waste, snow) are missing, so
  the shares are not citywide. See README "Why the API has gaps". (The 50/day figures
  above are from the original sample; the dashboard now uses a near-complete capture
  of the served types.)
- Times in the by-hour chart are **UTC** — subtract 4h for EDT before drawing
  push-notification conclusions.
- 2026 volume is partial. 2025 source/origin now resolves in the live API (checked
  2026-11-18), and 2025 has a method breakdown like other years.
