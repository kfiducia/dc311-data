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
`agg/smd_boundaries.min.geojson`) is **built in CI and published as a GitHub Pages
artifact — it is not committed to git.** This keeps the repo from growing megabytes
of regenerated JSON every month. Two exceptions are committed back to `main` once a
month (first run of the month, or any manual run): the raw per-year checkpoints in
`data/raw/` (so later runs only re-pull the current year), and the small
submission-volume seed: `agg.json`, its embedded copy in `dashboard.html`, and the
volume CSVs, ~0.5 MB in total (DCD19). Committing the seed stops a year that has
just ended from being re-pulled from ArcGIS every week. To add or change a radar
signal, edit `SIGNALS` in `pipeline/config.py`.

### Automated (weekly)

`.github/workflows/refresh.yml` runs **every Monday** (and via the
Actions tab's **Run workflow** button). It pulls fresh data straight from DC's
public ArcGIS API — no credentials needed — builds every artifact, sanity-gates
them, then deploys the whole site straight to Pages (`upload-pages-artifact` +
`deploy-pages`). Apart from the monthly commit described above, nothing is pushed
back to `main`:

```
python pipeline/fetch.py              # radar: every resolve_signals() service type + aux -> data/raw/
python pipeline/build.py              # radar aggregates -> agg/<signal>_alerts.json + signals.json
python pipeline/refresh_submission.py # submission volume -> agg.json + dashboards (current year live, past years from data/raw/)
python pipeline/categories.py         # complaint-category cube -> agg/categories.json
python pipeline/anomaly.py            # per-ward month-over-baseline board -> agg/anomalies.json
python pipeline/smd.py                # per-SMD counts + simplified boundaries -> agg/smd*.json
python pipeline/rats.py               # rat-report export for the abatement coordinator -> agg/rats/
python pipeline/guardrail.py          # fail (=> no deploy, last-good site stays live) if a build is empty/degenerate
```

The workflow caches the per-year checkpoint CSVs (`actions/cache` on `data/raw/`)
and re-pulls only the current, still-growing year each run (radar via `fetch.py`,
SMD via `smd.py`'s fetch manifest). `fetch.py` also re-pulls any past year whose
live count changed, and `refresh_submission.py` rebuilds every 2016+ year's volume
from those checkpoints, so a year that was still growing when it was last refreshed
gets finished. The guardrail fails the deploy if any past year's published total
is more than 1% off DC's live count (DCD16). `agg.json`, `agg/<signal>_alerts.json`,
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
python pipeline/refresh_methods.py      # reads ~/github/home-rule-org/dc311/data
```

### Where the enrichment code lives (not in this repo)

The scripts that build the enriched CSVs live **outside this repo, deliberately**:
in the local, uncommitted workspace `~/github/home-rule-org/dc311/`. `scripts/`
holds the code, `data/` holds the outputs (`dc311_sources_<year>.csv`, plus
`_backups/`), and `NOTES.md` / `HANDOFF_ANALYSIS.md` document DC's API.

| Script | What it does |
|---|---|
| `enrich_complete.py` | **The one to use.** Pages DC's live 311 API (`/311/v4/requests.json?start_date&end_date`, 50 rows per call) backward through each day to capture every SR the API serves. `YEARS=2026 python3 enrich_complete.py`; `DAY_STRIDE=n` samples every nth day. Resumable per day. |
| `enrich_sources.py`, `enrich_daterange.py`, `enrich_2025_daterange.py` | Older sampling enrichers (N SR ids/day looked up one by one). Superseded. |
| `dc311_download.py` | Bulk ArcGIS → `data/dc311_<year>.jsonl` (superseded here by `pipeline/fetch.py`). |
| `aggregate_submissions.py` | The original aggregator that `pipeline/refresh_methods.py` copies its method logic from. |

Things to know before rebuilding (checked 2026-11-18):
- **The API returns the full SR record.** That includes description, source/origin,
  status and dates, ANC/SMD/ward, agency, SLA, closure reason and `media_url`
  (reporter contact is redacted). `enrich_complete.py` keeps only 6 of those
  columns, so for full detail, change its `writerow` to dump the whole JSON.
- **It serves only about half of ArcGIS, and the gap is by request type.** It
  isn't random, and it isn't the pager. See "Why the API has gaps" below.
- **Cost:** ~16 calls / ~45 s per day, so ~4–5 h per year. Keep it sequential;
  ~8 concurrent requests get a 403. It needs a browser User-Agent (Cloudflare).
- **2025 now resolves.** `NOTES.md`'s "2025 gap" is out of date.
- **The API key is the public one DC's own 311 app embeds.** It's in the scripts.
  Don't commit it here, because this repo is public.

#### Why the API has gaps

The live API returned 115,645 SRs for 2026-01-01..06-17, vs 232,607 in
ArcGIS for the same days (49.7%). The gap is almost entirely **which request type
it is**:

- **60 service types are never served (0%).** That's 106k SRs, about 46% of
  ArcGIS. Largest: Parking Enforcement (26.6k), Scheduled Yard Waste (16.7k),
  DC Government Information (15.8k), Snow & Ice Removal on Public Space (9.3k),
  Residential Parking Permit Violation (7.1k), Sanitation Enforcement (6.3k),
  Snow Hero / ServeDC (4.5k), ROSA out-of-state parking (3.6k), Abandoned
  Vehicle on public property, Dockless Vehicle Parking, Missed Compost, DMV.
- **50 types are served essentially in full (≥90%, most 99–100%).** That's 110k
  SRs: bulk, missed trash and recycling, potholes, signals, signs, trees,
  sidewalks, dead animals, and so on.
- **Rodents (S0311) and Illegal Dumping (S0181) switch on in mid-2026.** Both
  are 0% before ~2026-05-01 (dumping from 04-30) and ~99–100% after. That makes
  their half-year numbers look partial (41% / 34%).
- **The missing SRs don't exist in the API at all.** Pre-cutover rodent and
  dumping ids, and sampled parking/yard-waste/DMV ids, all return an empty array
  when looked up by `service_request_id`. So no pager or query change can
  recover them.
- **Spot-checked across years (2026-11-18).** Fresh pulls for 2024-03-12,
  2025-08-20 and 2026-10-15 cover 48.7%, 49.6% and 50.8% of ArcGIS. On each day:
  - every served type matched ArcGIS **exactly** (100%);
  - the same never-served types (parking, RPP, ROSA, yard waste, info, DMV,
    sanitation enforcement, abandoned vehicles …) were 0;
  - rodents and dumping were 0 in 2024 and 2025 and complete in Oct 2026.

  Lookups by id for 2024 and 2025 agreed: served types are found, and
  never-served types and pre-cutover rodent ids are empty. The per-day count of
  zero-coverage types (28–36) is lower than 60 only because small types file
  nothing on a given day.

The most likely explanation (an inference, not confirmed with DC) is that this
API only fronts requests handled in DC's current Salesforce system. Agency
types routed to other systems (parking enforcement and DMV, info calls,
seasonal yard waste and snow programs) never land there, and rodent and dumping
moved over at the 2026-05-01 cutover. That's the same date the native-app
`source` step-change question in `submission_methods.md` centres on. In
practice:

- **Method/source shares describe only the served types.** They are not all of
  311. Phone-heavy types like DC Government Information and parking are absent.
- **Compare a type only over spans where its coverage is flat.** Rodent and
  dumping data from the API is usable from 2026-05-01 on.
- **Re-check before reusing these numbers.** Coverage is a moving target as DC
  migrates types: rerun the per-`service_code` API-vs-ArcGIS comparison.

Cadence is **weekly**, so the abatement coordinator's rat history stays within
about a week of DC. Raw checkpoints are still committed only **once a month**
(scheduled runs in the month's first week; manual runs always commit) to keep the
git history from growing four times faster.
