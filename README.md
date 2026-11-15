# DC 311 — Submission Methods & Rates

Interactive analysis of how DC residents file 311 service requests (phone, mobile
web, desktop web, native app) and submission-volume trends.

**Live dashboard:** https://kfiducia.github.io/dc311-data/

- `dashboard.html` — submission methods + volume, plus two segmentation views
  drawn from CityCast's neighborhood-311 analysis: a complaint-**category**
  breakdown by ward (top types, with the ever-present trash/parking/info
  categories toggle-able), and a per-ward **anomaly board** — for the latest
  complete month, which category is running most above that ward's own
  same-month-in-prior-years normal (the reporter's method, run over every
  category)
- `early-warning.html` — **Complaint Radar**: one seasonal-anomaly detector,
  selectable across signals **chosen data-driven** — rats (special, with a
  dead-animal overlay) plus the top-N categories by volume, minus the ubiquitous
  ones (`config.resolve_signals`, `RADAR_TOP_N`). The fine-grained "activity over
  time" heatmap is built for the rat signal only; other signals omit it (keeps the
  payload small — see DCD8)
- `submission_methods.md` — written findings
- `export_*.csv` — underlying aggregate tables
- `agg.json` — aggregated volume/method data behind the dashboard
- `agg/signals.json` + `agg/<signal>_alerts.json` — radar manifest + per-signal
  detections
- `agg/categories.json` — per-year, per-ward complaint-category cube
- `agg/anomalies.json` — per-ward month-over-baseline anomaly board
- `smd.html` + `agg/smd.json` + `agg/smd_boundaries.min.geojson` — per-SMD (ANC
  Single Member District) ranked chart + choropleth map
- `agg/rats/` — rat-report export for the
  [abatement coordinator](https://abatement.home-rule.org) (`pipeline/rats.py`):
  per-year rat reports with their SMD, per-SMD rankings, and city blocks with
  recent-report counts. **A published contract** — that site's cron polls
  `agg/rats/meta.json` and reloads when `generated` changes; bump `SCHEMA` in
  `rats.py` on any breaking change

Volume figures use the full DC ArcGIS bulk dataset (4.97M requests, 2009–2026).
Submission-method percentages are from per-request `source`/`origin` lookups
against DC's live 311 API (sampled). All data is aggregate DC public-records data.

## Refresh

The dashboards are static, and the machine-generated data (`agg/*.json`,
`agg/smd_boundaries.min.geojson`, and the volume half of `agg.json`) is **built in
CI and published as a GitHub Pages artifact — it is not committed to git.** This
keeps the repo from growing megabytes of regenerated JSON every month. `main` holds
only source (HTML, `pipeline/`, the method-data seed); the built data lives in the
deployed artifact. To add or change a radar signal, edit `SIGNALS` in
`pipeline/config.py`.

### Automated (weekly)

`.github/workflows/refresh.yml` runs **every Monday** (and via the
Actions tab's **Run workflow** button). It pulls fresh data straight from DC's
public ArcGIS API — no credentials needed — builds every artifact, sanity-gates
them, then deploys the whole site straight to Pages (`upload-pages-artifact` +
`deploy-pages`); nothing is pushed back to `main`:

```
python pipeline/fetch.py              # radar: every resolve_signals() service type + aux -> data/raw/
python pipeline/build.py              # radar aggregates -> agg/<signal>_alerts.json + signals.json
python pipeline/refresh_submission.py # submission volume (current year) -> agg.json + dashboards
python pipeline/categories.py         # complaint-category cube -> agg/categories.json
python pipeline/anomaly.py            # per-ward month-over-baseline board -> agg/anomalies.json
python pipeline/smd.py                # per-SMD counts + simplified boundaries -> agg/smd*.json
python pipeline/rats.py               # rat-report export for the abatement coordinator -> agg/rats/
python pipeline/guardrail.py          # fail (=> no deploy, last-good site stays live) if a build is empty/degenerate
```

The workflow caches the per-year checkpoint CSVs (`actions/cache` on `data/raw/`)
and re-pulls only the current, still-growing year each run (radar via `fetch.py`,
SMD via `smd.py`'s fetch manifest). `agg.json`, `agg/<signal>_alerts.json`,
`agg/categories.json`, and `agg/smd.json` carry `generated`/`generated_at` +
`data_through` stamps, surfaced in each dashboard's footer so staleness is visible.

**Pages source must be set to "GitHub Actions"** (repo Settings → Pages), not
"Deploy from branch" — the artifact deploy requires it.

### Local build

Because the data isn't committed, building the site locally means running the
pipeline first:

```
cd pipeline && python fetch.py && python build.py && python refresh_submission.py \
  && python categories.py && python anomaly.py && python smd.py && python rats.py
python pipeline/guardrail.py
# then serve the repo root: python -m http.server 8000
```

### Manual — submission method/source data

The submission **method / source** numbers (phone vs. mobile vs. web) are *not*
part of the automated refresh: they come from externally-enriched CSVs in a
separate local workspace (`pipeline/refresh_methods.py`, path via `SRC_DIR`),
which CI doesn't have — running it without those CSVs would overwrite good method
data with empties. Refresh them by hand when the enriched CSVs are updated:

```
SRC_DIR=/path/to/snap311/.../data python pipeline/refresh_methods.py
```

Cadence is **weekly**, so the abatement coordinator's rat history stays within
about a week of DC. Raw checkpoints are still committed only **once a month**
(scheduled runs in the month's first week; manual runs always commit) to keep the
git history from growing four times faster.
