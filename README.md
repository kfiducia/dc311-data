# DC 311 — Submission Methods & Rates

Interactive analysis of how DC residents file 311 service requests (phone, mobile
web, desktop web, native app) and submission-volume trends.

**Live dashboard:** https://kfiducia.github.io/dc311-data/

- `dashboard.html` — submission methods + volume, plus a complaint-**category**
  breakdown by ward (top types, with the ever-present trash/parking/info
  categories toggle-able so local outliers surface)
- `early-warning.html` — **Complaint Radar**: one seasonal-anomaly detector over
  several 311 signals (rats, DMV, dockless vehicles, trash-cart repair),
  selectable from a picker
- `submission_methods.md` — written findings
- `export_*.csv` — underlying aggregate tables
- `agg.json` — aggregated volume/method data behind the dashboard
- `agg/signals.json` + `agg/<signal>_alerts.json` — radar manifest + per-signal
  detections
- `agg/categories.json` — per-year, per-ward complaint-category cube

Volume figures use the full DC ArcGIS bulk dataset (4.97M requests, 2009–2026).
Submission-method percentages are from per-request `source`/`origin` lookups
against DC's live 311 API (sampled). All data is aggregate DC public-records data.

## Refresh

The dashboards are static: they render committed artifacts (`agg.json`, the
per-signal `agg/<signal>_alerts.json` + `agg/signals.json` manifest, the
`agg/categories.json` category cube, and `export_*.csv`), so "refresh the data"
means re-run the pipeline and commit the regenerated files. GitHub Pages redeploys
on push. To add or change a radar signal, edit `SIGNALS` in `pipeline/config.py`.

### Automated (monthly)

`.github/workflows/refresh.yml` runs on the **1st of each month** (and via the
Actions tab's **Run workflow** button). It pulls fresh data straight from DC's
public ArcGIS API — no credentials needed — regenerates the artifacts, runs a
freshness guardrail, and commits only if something changed:

```
python pipeline/fetch.py              # radar: every SIGNALS service type + aux -> data/raw/
python pipeline/build.py              # radar aggregates -> agg/<signal>_alerts.json + signals.json
python pipeline/refresh_submission.py # submission volume (current year) -> agg.json + dashboards
python pipeline/categories.py         # complaint-category cube -> agg/categories.json
python pipeline/guardrail.py          # fail if a pull is truncated/empty or the window regressed
```

The workflow caches the per-year checkpoint CSVs (`actions/cache` on `data/raw/`)
and re-pulls only the current, still-growing year each run. `agg.json`,
`agg/<signal>_alerts.json`, and `agg/categories.json` carry `generated`/
`generated_at` + `data_through` stamps, surfaced in each dashboard's footer so
staleness is visible at a glance.

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
