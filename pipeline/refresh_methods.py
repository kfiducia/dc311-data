"""Refresh the submission dashboard's SOURCE/METHOD numbers from the enriched
DC 311 CSVs, without touching the (separately-refreshed) volume data.

The canonical aggregator (snap311/.../aggregate_submissions.py) streams 4.3 GB of
bulk jsonl for volume AND reads the small enriched CSVs for source/method. Volume
is already refreshed live by refresh_submission.py, so here we reproduce ONLY the
method half — verbatim logic — and splice the fresh method fields into agg.json +
the embedded dashboards. Fast (CSVs only).

Enriched CSVs live in the snap311 analysis workspace; override with SRC_DIR.
"""
import csv
import glob
import json
import os
import re
from collections import Counter, defaultdict
from datetime import date as _date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = Path(os.environ.get(
    "SRC_DIR", "/Users/kfiducia/GitHub/snap311/dc311/dc311/data"))
DC_TZ = ZoneInfo("America/New_York")

# ---- method aggregation (verbatim from aggregate_submissions.py) ----------
def channel(src):
    s = src.lower()
    if s in ("agent", "aws connect - production") or "connect" in s or "ivr" in s:
        return "Phone / Call center"
    if src in ("iOS", "Android") or "browser" in s or s == "mobile":
        return "Mobile (app + web)"
    if s in ("web", "portal"):
        return "Desktop web"
    return "Other"


def new_year_agg():
    return {"source_counts": Counter(), "origin_counts": Counter(),
            "source_by_week": defaultdict(Counter),
            "source_by_service": defaultdict(Counter),
            "source_by_hour": defaultdict(Counter),
            "source_by_hour_local": defaultdict(Counter),
            "source_by_dow": defaultdict(Counter), "enriched_n": 0}


def sr_year(row):
    sid = (row.get("service_request_id") or "").strip()
    if len(sid) >= 2 and sid[:2].isdigit():
        return f"20{sid[:2]}"
    ts = (row.get("requested_datetime") or "").strip()
    return ts[:4] if ts[:4].isdigit() else "unknown"


def add_row(agg, row):
    src = (row.get("source") or "").strip() or "Unknown"
    org = (row.get("origin") or "").strip() or "Unknown"
    agg["source_counts"][src] += 1
    agg["origin_counts"][org] += 1
    agg["enriched_n"] += 1
    svc = (row.get("service_name") or "").strip() or "Unknown"
    agg["source_by_service"][svc][src] += 1
    ts = (row.get("requested_datetime") or "").strip()
    if ts:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            iso = dt.isocalendar()
            agg["source_by_week"][f"{iso[0]:04d}-W{iso[1]:02d}"][src] += 1
            agg["source_by_hour"][src][dt.hour] += 1
            agg["source_by_hour_local"][src][dt.astimezone(DC_TZ).hour] += 1
            agg["source_by_dow"][src][dt.weekday()] += 1
        except ValueError:
            pass


def channel_counts_of(agg):
    c = Counter()
    for src, n in agg["source_counts"].items():
        c[channel(src)] += n
    return c


def serialize_methods(agg):
    utc = Counter()
    for c in agg["source_by_hour"].values():
        utc.update(c)
    tot = sum(utc.values()) or 1
    evening = sum(utc[h] for h in (21, 22, 23)) / tot
    return {
        "enriched_n": agg["enriched_n"],
        "source_counts": agg["source_counts"].most_common(),
        "origin_counts": agg["origin_counts"].most_common(),
        "channel_counts": channel_counts_of(agg).most_common(),
        "source_by_week": {w: dict(c) for w, c in sorted(agg["source_by_week"].items())},
        "source_by_service": {s: dict(c) for s, c in agg["source_by_service"].items()},
        "source_by_hour": {s: dict(c) for s, c in agg["source_by_hour"].items()},
        "source_by_hour_local": {s: dict(c) for s, c in agg["source_by_hour_local"].items()},
        "source_by_dow": {s: dict(c) for s, c in agg["source_by_dow"].items()},
        "intraday_unbiased": tot >= 500 and evening < 0.40,
    }


def file_year(path):
    m = re.search(r"dc311_sources_(\d{4})", os.path.basename(path))
    return m.group(1) if m else None


def max_date(fp):
    mx = ""
    with open(fp) as fh:
        for row in csv.DictReader(fh):
            ts = (row.get("requested_datetime") or "")[:10]
            if ts > mx:
                mx = ts
    return mx


def fresh_complete(fp, year, today):
    mx = max_date(fp)
    if not mx:
        return False
    target = min(_date(int(year), 12, 31), today)
    return (target - _date.fromisoformat(mx)).days <= 3


def main():
    today = datetime.now(timezone.utc).date()
    fresh = {file_year(fp): fp for fp in sorted(glob.glob(str(SRC_DIR / "dc311_sources_*.csv"))) if file_year(fp)}
    old = {file_year(fp): fp for fp in sorted(glob.glob(str(SRC_DIR / "_backups" / "dc311_sources_*_old.csv"))) if file_year(fp)}
    src_by_year, old_year_set = {}, set()
    for y in sorted(set(fresh) | set(old)):
        if y in fresh and fresh_complete(fresh[y], y, today):
            src_by_year[y] = fresh[y]
        elif y in old:
            src_by_year[y] = old[y]; old_year_set.add(y)
        elif y in fresh:
            src_by_year[y] = fresh[y]

    years = defaultdict(new_year_agg)
    allyr = new_year_agg()
    for y, fp in sorted(src_by_year.items()):
        with open(fp) as fh:
            for row in csv.DictReader(fh):
                add_row(years[sr_year(row)], row)
                add_row(allyr, row)

    methods = {y: serialize_methods(a) for y, a in years.items()}
    methods["all"] = serialize_methods(allyr)

    # ---- splice fresh method fields into the live-volume agg.json ----
    agg = json.loads((ROOT / "agg.json").read_text())
    agg["method_years"] = sorted(y for y in methods if y != "all")
    agg["stale_method_years"] = sorted(old_year_set)
    agg["methods"] = methods
    agg["enriched_n"] = allyr["enriched_n"]
    agg["source_counts"] = allyr["source_counts"].most_common()
    agg["origin_counts"] = allyr["origin_counts"].most_common()
    agg["channel_counts"] = channel_counts_of(allyr).most_common()
    agg["generated"] = datetime.now(timezone.utc).isoformat()
    (ROOT / "agg.json").write_text(json.dumps(agg, separators=(",", ":")))

    # convenience export CSVs
    def write_csv(name, header, rows):
        with (ROOT / name).open("w", newline="") as fh:
            w = csv.writer(fh); w.writerow(header); w.writerows(rows)
    write_csv("export_source_counts.csv", ["source", "count"], agg["source_counts"])
    write_csv("export_origin_counts.csv", ["origin", "count"], agg["origin_counts"])
    write_csv("export_channel_counts.csv", ["channel", "count"], agg["channel_counts"])

    # re-embed DATA blob in dashboard.html (and index.html if it still holds it)
    blob = "const DATA = " + json.dumps(agg, separators=(",", ":")) + ";"
    for fn in ("dashboard.html", "index.html"):
        p = ROOT / fn
        lines = p.read_text().split("\n")
        for i, l in enumerate(lines):
            if l.startswith("const DATA"):
                lines[i] = blob
                p.write_text("\n".join(lines))
                print(f"  updated embedded DATA in {fn}")
                break

    print(f"Done. enriched_n={agg['enriched_n']:,}, method_years={agg['method_years']}")
    print(f"  2026 sources: {methods.get('2026', {}).get('source_counts', [])[:6]}")


if __name__ == "__main__":
    main()
