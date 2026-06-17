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
2009   21,617      2015  298,413      2021  360,842
2010   30,199      2016  304,475      2022  386,708
2011   10,550      2017  311,437      2023  425,262   <- peak
2012  260,608      2018  338,903      2024  398,030
2013  235,024      2019  367,055      2025  356,000
2014  325,538      2020  305,612      2026  230,380   <- partial (thru mid-June)
```

- System matured 2012 (jump from ~10k to ~260k once intake was digitized).
- Steady ~300–425k/yr since. 2023 peak, slight softening 2024–25.
- 2026 at 230k through ~mid-June annualizes to ~490k — **on pace for a record year**,
  worth confirming once the year completes.
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

## Files

- `dashboard.html` — interactive charts (open in browser)
- `agg.json` — all aggregated data
- `export_by_year.csv`, `export_by_month.csv` — volume/rates
- `export_source_counts.csv`, `export_channel_counts.csv`, `export_origin_counts.csv` — methods
- `export_source_by_service.csv` — method × service crosstab
- `scripts/aggregate_submissions.py`, `scripts/build_dashboard.py` — regenerate everything

## Caveats

- Method % are from a 50/day sample, not the full population. Proportions are solid;
  weekly/sub-category splits get thin fast.
- Times in the by-hour chart are **UTC** — subtract 4h for EDT before drawing
  push-notification conclusions.
- 2026 volume is partial. 2025 bulk data exists despite the handoff note (the live
  API still won't return 2025 source/origin, so 2025 has no method breakdown).
