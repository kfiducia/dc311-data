"""Refresh the SUBMISSION dashboard's volume data to today.

The submission dashboard (dashboard.html / index.html) embeds all its data as a
`const DATA = {...}` blob (== agg.json). This rebuilds the volume slices —
by_year / by_month / month_<Y> / week_<Y> / by_year_ward / hour_local_by_year —
plus bulk_total + generated. The externally sampled *method* data
(methods/source/origin/channel) is left untouched.

Where each year's numbers come from:
  * current year — pulled live from DC's ArcGIS API (as before).
  * past years with a raw checkpoint (data/raw/_all_<Y>.csv, 2016+) — year
    total / months / wards are recomputed from the checkpoint every run. fetch.py
    keeps those complete (it re-pulls any year whose live count changed), so a
    year that was still growing when it was last refreshed gets finished here.
    DCD16: previously only the current year was ever refreshed, which froze 2024
    at its Dec-4 snapshot and 2025 at its Oct-6 snapshot.
  * hour_local_by_year for those past years — the checkpoints only carry the UTC
    *date*, not the time, so hours can't be derived from them. The seed's hours
    are kept unless they no longer add up to the year's total (>1% off), in which
    case that year is re-pulled from ArcGIS once to rebuild them.
  * 2009–2015 — no raw checkpoint; kept as-is from the committed seed.

Counts come from the "All Service Requests - <year>" layer (ALL requests, not
just rodents), bucketed by UTC date. Hour-of-day is bucketed in DC local time.

Run:  ./.venv/bin/python pipeline/refresh_submission.py
(Basis for the future auto-refresh; safe to re-run — it's idempotent.)
"""
import csv
import json
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import config as C

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
NY = ZoneInfo("America/New_York")
# Seeded hour-of-day sums / re-pulled totals may drift this far from the raw
# checkpoint total before we distrust them (guardrail.VOLUME_TOL uses the same 1%).
HOURS_TOL = 0.01


def _get(url, params):
    q = urllib.parse.urlencode(params)
    for attempt in range(6):
        try:
            with urllib.request.urlopen(f"{url}?{q}", timeout=120) as r:
                data = json.load(r)
            # ArcGIS reports failures (e.g. an overloaded server) as HTTP 200 with
            # an "error" body and no features — which the pager would otherwise
            # read as end-of-data and silently truncate the year.
            if "error" in data:
                raise RuntimeError(f"ArcGIS error: {data['error']}")
            return data
        except Exception as e:  # noqa: BLE001
            if attempt == 5:
                raise
            print(f"  retry {attempt+1} ({e})", file=sys.stderr)
            time.sleep(3 * (attempt + 1))


def year_layers():
    meta = _get(C.ARCGIS_SERVICE, {"f": "json"})
    out = {}
    for lyr in meta["layers"]:
        name = lyr["name"]
        if name.startswith("All Service Requests - "):
            tail = name.rsplit("-", 1)[-1].strip()
            if tail.isdigit():
                out[int(tail)] = lyr["id"]
    return out


def fetch_year_records(layer_id, year):
    """All requests for the year: (utc_datetime, ward). Paginated."""
    rows, offset = [], 0
    while True:
        data = _get(
            f"{C.ARCGIS_SERVICE}/{layer_id}/query",
            {"where": "1=1", "outFields": "ADDDATE,WARD", "returnGeometry": "false",
             "resultOffset": offset, "resultRecordCount": 1000,
             "orderByFields": "ADDDATE", "f": "json"},
        )
        feats = data.get("features", [])
        if not feats:
            break
        for f in feats:
            a = f["attributes"]
            ms = a.get("ADDDATE")
            if ms is None:
                continue
            dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
            rows.append((dt, (a.get("WARD") or "").strip()))
        offset += len(feats)
        if offset % 20000 == 0:
            print(f"  {year}: {offset} rows", file=sys.stderr)
        if len(feats) < 1000:
            break
    return rows


def compute_year(records, year):
    """Consistent slices for one calendar year (by UTC date; hour in DC local)."""
    months, weeks, wards = Counter(), Counter(), Counter()
    hours = [0] * 24
    total = 0
    for dt, ward in records:
        d = dt.date()
        if d.year != year:
            continue  # drop the rare boundary record so totals stay consistent
        total += 1
        months[d.strftime("%Y-%m")] += 1
        iso = dt.isocalendar()
        if iso.year == year:
            weeks[f"{year}-W{iso.week:02d}"] += 1
        wards[ward or "Unknown"] += 1
        hours[dt.astimezone(NY).hour] += 1
    month_list = sorted(months.items())
    week_list = sorted(weeks.items())
    ward_list = sorted(wards.items(), key=lambda kv: -kv[1])
    return total, month_list, week_list, ward_list, hours


def raw_year(year):
    """(total, month_list, week_list, ward_list) for a past year from its raw
    checkpoint, or None if there is no checkpoint. Same UTC-date bucketing as
    compute_year."""
    p = RAW / f"_all_{year}.csv"
    if not p.exists():
        return None
    days, wards = Counter(), Counter()
    with p.open(newline="") as fh:
        for r in csv.DictReader(fh):
            d = r["date"]
            if not d.startswith(f"{year}-"):
                continue  # dateless / boundary rows, as in compute_year
            days[d] += 1
            wards[r["ward"].strip() or "Unknown"] += 1
    months, weeks = Counter(), Counter()
    for d, n in days.items():
        months[d[:7]] += n
        iso = date.fromisoformat(d).isocalendar()
        if iso.year == year:
            weeks[f"{year}-W{iso.week:02d}"] += n
    return (sum(days.values()), sorted(months.items()), sorted(weeks.items()),
            sorted(wards.items(), key=lambda kv: -kv[1]))


def set_year(agg, year, total, month_list, ward_list, hours=None):
    """Swap one year's volume slices into agg (by_year / by_month / by_year_ward,
    and hour_local_by_year when given)."""
    ys = str(year)
    agg["by_year"] = [[y, total if y == year else c] for y, c in agg["by_year"]]
    if year not in [y for y, _ in agg["by_year"]]:
        agg["by_year"].append([year, total])
        agg["by_year"].sort()
    agg["by_month"] = [[m, c] for m, c in agg["by_month"]
                       if not m.startswith(ys + "-")] + [list(x) for x in month_list]
    agg["by_month"].sort()
    agg["by_year_ward"][ys] = [list(x) for x in ward_list]
    if hours is not None:
        agg["hour_local_by_year"][ys] = hours


def main():
    agg = json.loads((ROOT / "agg.json").read_text())
    layers = year_layers()
    year = max(layers)  # current year

    # Past years: recompute from the (complete) raw checkpoints.
    for y in sorted(layers):
        if y == year:
            continue
        got = raw_year(y)
        if got is None:
            continue
        total, month_list, week_list, ward_list = got
        old = dict(agg["by_year"]).get(y)
        hours = agg["hour_local_by_year"].get(str(y))
        if not hours or abs(sum(hours) - total) > HOURS_TOL * total:
            print(f"  {y}: hour-of-day seed ({sum(hours or []):,}) is stale vs "
                  f"{total:,} — re-pulling from ArcGIS …")
            pulled, _, _, _, fresh = compute_year(fetch_year_records(layers[y], y), y)
            if abs(pulled - total) > HOURS_TOL * total:
                # Hours are a cosmetic chart: keep the old ones (guardrail warns)
                # rather than fail the run and block every other artifact's deploy.
                print(f"  WARNING {y}: re-pull returned {pulled:,} rows vs {total:,} "
                      "in the raw checkpoint — keeping previous hours", file=sys.stderr)
            else:
                hours = fresh
        set_year(agg, y, total, month_list, ward_list, hours)
        # A year that was "current" in the seed carries month_<Y>/week_<Y> slices
        # (the dashboard charts month_<Y>); finish them too, or they stay frozen at
        # the snapshot taken while the year was still growing.
        if f"month_{y}" in agg:
            agg[f"month_{y}"] = [list(x) for x in month_list]
        if f"week_{y}" in agg:
            agg[f"week_{y}"] = [list(x) for x in week_list]
        if old != total:
            was = f"{old:,}" if old is not None else "(absent)"
            print(f"  {y}: {was} → {total:,} (from raw checkpoint)")

    print(f"Refreshing submission dashboard for {year} …")
    recs = fetch_year_records(layers[year], year)
    total, month_list, week_list, ward_list, hours = compute_year(recs, year)
    print(f"  {year}: {total:,} requests · {len(month_list)} months "
          f"(latest {month_list[-1][0]}={month_list[-1][1]:,})")

    set_year(agg, year, total, month_list, ward_list, hours)
    agg[f"month_{year}"] = [list(x) for x in month_list]
    agg[f"week_{year}"] = [list(x) for x in week_list]
    agg["bulk_total"] = sum(c for _, c in agg["by_year"])
    agg["generated"] = datetime.now(timezone.utc).isoformat()
    # Freshness stamp: latest month present in the (now-refreshed) volume series.
    agg["data_through"] = agg["by_month"][-1][0] if agg["by_month"] else None

    # write agg.json
    (ROOT / "agg.json").write_text(json.dumps(agg, separators=(",", ":")))
    # write the volume CSVs
    with (ROOT / "export_by_month.csv").open("w", newline="") as fh:
        w = csv.writer(fh); w.writerow(["month", "requests"]); w.writerows(agg["by_month"])
    with (ROOT / "export_by_year.csv").open("w", newline="") as fh:
        w = csv.writer(fh); w.writerow(["year", "requests"]); w.writerows(agg["by_year"])

    # splice the fresh DATA blob into the embedded dashboards
    blob = "const DATA = " + json.dumps(agg, separators=(",", ":")) + ";"
    for fn in ("dashboard.html", "index.html"):
        p = ROOT / fn
        lines = p.read_text().split("\n")
        hit = False
        for i, l in enumerate(lines):
            if l.startswith("const DATA"):
                lines[i] = blob
                hit = True
                break
        if hit:
            p.write_text("\n".join(lines))
            print(f"  updated embedded DATA in {fn}")
        else:
            print(f"  NOTE: no `const DATA` line in {fn} (skipped)")

    print(f"Done. bulk_total={agg['bulk_total']:,}, generated={agg['generated']}")


if __name__ == "__main__":
    main()
