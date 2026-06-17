# DC 311 — Submission Methods & Rates

Interactive analysis of how DC residents file 311 service requests (phone, mobile
web, desktop web, native app) and submission-volume trends.

**Live dashboard:** https://kfiducia.github.io/dc311-data/

- `dashboard.html` — interactive charts (year selector: 2024 / 2026 / combined)
- `submission_methods.md` — written findings
- `export_*.csv` — underlying aggregate tables
- `agg.json` — all aggregated data behind the dashboard

Volume figures use the full DC ArcGIS bulk dataset (4.97M requests, 2009–2026).
Submission-method percentages are from per-request `source`/`origin` lookups
against DC's live 311 API (sampled). All data is aggregate DC public-records data.
