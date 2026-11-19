"""Refresh the submission dashboard's SOURCE/METHOD numbers from the enriched
DC 311 CSVs (data/sources/<year>.csv, from enrich_sources.py), without touching
the (separately-refreshed) volume data.

Two halves, so the weekly volume build stays the only writer of agg.json:
  --distill  CSVs -> data/methods.json (committed). Run by methods.yml on the
             homelab k3s runner right after enrich_sources.py. Refuses to write
             if any year's count would shrink (a stale/partial CSV).
  --splice   data/methods.json -> agg.json method fields + the export CSVs + the
             embedded DATA in the dashboards. Run by refresh.yml after
             refresh_submission.py. No methods.json = leave agg.json as-is.
  (none)     both, for a local run.

The method aggregation is verbatim from the original aggregator
(home-rule-org/dc311/scripts/aggregate_submissions.py). Override the CSV dir
with SRC_DIR.
"""
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = Path(os.environ.get("SRC_DIR", ROOT / "data" / "sources"))
METHODS = ROOT / "data" / "methods.json"
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


def max_date(fp):
    mx = ""
    with open(fp) as fh:
        for row in csv.DictReader(fh):
            ts = (row.get("requested_datetime") or "")[:10]
            if ts > mx:
                mx = ts
    return mx


def distill():
    """CSVs -> the method fields (+ provenance), refusing to shrink any year."""
    src = {m.group(1): fp for fp in sorted(SRC_DIR.glob("*.csv"))
           if (m := re.fullmatch(r"(\d{4})\.csv", fp.name))}
    if not src:
        raise SystemExit(f"no <year>.csv files in {SRC_DIR}")
    years = defaultdict(new_year_agg)
    allyr = new_year_agg()
    seen = set()  # an SR at exactly 00:00:00 could land in two days' windows (pre-2026-11 capture)
    for y, fp in sorted(src.items()):
        with open(fp) as fh:
            for row in csv.DictReader(fh):
                if row["service_request_id"] in seen:
                    continue
                seen.add(row["service_request_id"])
                add_row(years[sr_year(row)], row)
                add_row(allyr, row)

    methods = {y: serialize_methods(a) for y, a in years.items()}
    methods["all"] = serialize_methods(allyr)
    out = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "through": {y: max_date(fp) for y, fp in sorted(src.items())},
        "method_years": sorted(y for y in methods if y != "all"),
        "stale_method_years": [],
        "methods": methods,
        "enriched_n": allyr["enriched_n"],
        "source_counts": allyr["source_counts"].most_common(),
        "origin_counts": allyr["origin_counts"].most_common(),
        "channel_counts": channel_counts_of(allyr).most_common(),
    }

    # The captures are append-only, so a year never legitimately loses rows. A
    # drop means a stale or partial CSV (2026-07: a refresh read a machine's
    # older copy and would have cut 2026 from 134k to 116k). Refuse it.
    prev = json.loads(METHODS.read_text()) if METHODS.exists() else \
        json.loads((ROOT / "agg.json").read_text())
    shrunk = {y: (m["enriched_n"], methods[y]["enriched_n"])
              for y, m in prev.get("methods", {}).items()
              if y in methods and methods[y]["enriched_n"] < m["enriched_n"]}
    missing = sorted(set(prev.get("method_years", [])) - set(out["method_years"]))
    if (shrunk or missing) and not os.environ.get("ALLOW_SHRINK"):
        raise SystemExit(f"refusing to write {METHODS.name}: years shrank {shrunk} / "
                         f"missing {missing} (stale sources?). ALLOW_SHRINK=1 to override.")
    # Keep the old file (and its `generated`) when nothing else changed, so a run
    # with no new days doesn't commit a timestamp-only diff.
    # (compare as JSON: `out` holds tuples and int keys where the file has lists/strings)
    if {**prev, "generated": None} == {**json.loads(json.dumps(out)), "generated": None}:
        print(f"{METHODS.relative_to(ROOT)} unchanged (through {max(out['through'].values())})")
        return prev
    METHODS.write_text(json.dumps(out, separators=(",", ":")))
    print(f"Distilled -> {METHODS.relative_to(ROOT)}: enriched_n={out['enriched_n']:,}, "
          f"through={out['through']}")
    return out


def splice(m):
    """Write the method fields into agg.json, the export CSVs and the embedded DATA."""
    agg = json.loads((ROOT / "agg.json").read_text())
    for k in ("method_years", "stale_method_years", "methods", "enriched_n",
              "source_counts", "origin_counts", "channel_counts"):
        agg[k] = m[k]
    agg["methods_through"] = max(m["through"].values())
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

    print(f"Spliced: enriched_n={agg['enriched_n']:,}, methods through "
          f"{agg['methods_through']}, method_years={agg['method_years']}")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "--distill":
        distill()
    elif mode == "--splice":
        if not METHODS.exists():
            print(f"no {METHODS.relative_to(ROOT)} — keeping agg.json's method data as-is")
            return
        splice(json.loads(METHODS.read_text()))
    elif mode == "":
        splice(distill())
    else:
        sys.exit(f"usage: {sys.argv[0]} [--distill | --splice]")


if __name__ == "__main__":
    main()
