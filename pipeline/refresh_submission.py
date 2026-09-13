"""Manually refresh the SUBMISSION dashboard's volume data to today.

The submission dashboard (dashboard.html / index.html) embeds all its data as a
`const DATA = {...}` blob (== agg.json). History (2009 .. last year) is immutable;
only the current year grows. So this refreshes just the current-year slices —
by_year / by_month / month_<Y> / week_<Y> / by_year_ward / hour_local_by_year —
plus bulk_total + generated, straight from DC's ArcGIS API. The externally
sampled *method* data (methods/source/origin/channel) is left untouched.

Counts come from the "All Service Requests - <year>" layer (ALL requests, not
just rodents). Hour-of-day is bucketed in DC local time.

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
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import config as C

ROOT = Path(__file__).resolve().parent.parent
NY = ZoneInfo("America/New_York")


def _get(url, params):
    q = urllib.parse.urlencode(params)
    for attempt in range(6):
        try:
            with urllib.request.urlopen(f"{url}?{q}", timeout=120) as r:
                return json.load(r)
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


def main():
    agg = json.loads((ROOT / "agg.json").read_text())
    layers = year_layers()
    year = max(layers)  # current year
    print(f"Refreshing submission dashboard for {year} …")
    recs = fetch_year_records(layers[year], year)
    total, month_list, week_list, ward_list, hours = compute_year(recs, year)
    print(f"  {year}: {total:,} requests · {len(month_list)} months "
          f"(latest {month_list[-1][0]}={month_list[-1][1]:,})")

    ys = str(year)
    # by_year: replace this year's total
    agg["by_year"] = [[y, total if y == year else c] for y, c in agg["by_year"]]
    if year not in [y for y, _ in agg["by_year"]]:
        agg["by_year"].append([year, total])
    # by_month: keep other years, swap in fresh current-year months
    agg["by_month"] = [[m, c] for m, c in agg["by_month"]
                       if not m.startswith(ys + "-")] + [list(x) for x in month_list]
    agg["by_month"].sort()
    agg[f"month_{year}"] = [list(x) for x in month_list]
    agg[f"week_{year}"] = [list(x) for x in week_list]
    agg["by_year_ward"][ys] = [list(x) for x in ward_list]
    agg["hour_local_by_year"][ys] = hours
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
