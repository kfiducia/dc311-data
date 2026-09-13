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

## Refresh

The dashboards are static: they render committed artifacts (`agg.json`,
`agg/rodent_alerts.json`, `export_*.csv`), so "refresh the data" means re-run the
pipeline and commit the regenerated files. GitHub Pages redeploys on push.

### Automated (monthly)

`.github/workflows/refresh.yml` runs on the **1st of each month** (and via the
Actions tab's **Run workflow** button). It pulls fresh data straight from DC's
public ArcGIS API — no credentials needed — regenerates the artifacts, runs a
freshness guardrail, and commits only if something changed:

```
python pipeline/fetch.py              # rat-radar: rodent + dead-animal reports -> data/raw/
python pipeline/build.py              # rat-radar aggregates -> agg/rodent_alerts.json
python pipeline/refresh_submission.py # submission volume (current year) -> agg.json + dashboards
python pipeline/guardrail.py          # fail if the pull is truncated/empty or the window regressed
```

The workflow caches the per-year checkpoint CSVs (`actions/cache` on `data/raw/`)
and re-pulls only the current, still-growing year each run. `agg.json` and
`agg/rodent_alerts.json` carry `generated`/`generated_at` + `data_through` stamps,
surfaced in each dashboard's footer so staleness is visible at a glance.

### Manual — submission method/source data

The submission **method / source** numbers (phone vs. mobile vs. web) are *not*
part of the automated refresh: they come from externally-enriched CSVs in a
separate local workspace (`pipeline/refresh_methods.py`, path via `SRC_DIR`),
which CI doesn't have — running it without those CSVs would overwrite good method
data with empties. Refresh them by hand when the enriched CSVs are updated:

```
SRC_DIR=/path/to/snap311/.../data python pipeline/refresh_methods.py
```

Cadence for the automated volume/rat-radar refresh is **monthly** — DC's data
lags and backfills prior months, so daily runs would be redundant.
