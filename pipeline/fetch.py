"""Pull ALL raw DC 311 records once per year from ArcGIS -> data/raw/_all_<year>.csv.

One row per request:  date, resolved, lat, lon, ward, code, service, agency
  (ADDDATE, RESOLUTIONDATE, LATITUDE, LONGITUDE, WARD, SERVICECODE,
   SERVICECODEDESCRIPTION, SERVICETYPECODEDESCRIPTION)

This single raw source feeds everything downstream (DCD10): build.py's radar
filters it by SERVICECODE; smd.py's choropleth point-in-polygons the rows and
groups by the granular SERVICECODEDESCRIPTION. It replaces the old per-signal
`_<key>_<year>.csv` fetches and smd.py's separate all-points pull — the full
dataset was being pulled once for SMD *plus* filtered subsets ~9 more times.
Rows missing lat/lon are KEPT (empty coords) so SMD's no-latlon bucket is honest;
rows missing a date are skipped (can't place them in time).

Committed to git (DCD8), so CI only ever fetches the CURRENT year: a year is
re-fetched only if its source record count changed (a backfill) — plus always the
current year, whose closures fill in without changing the count. An unknown count
trusts the committed checkpoint, so a fresh clone never re-pulls history. Change
token: data/raw/source_counts.json (per-year returnCountOnly — this MapServer has
no reliable lastEditDate/ETag).
"""
import csv
import json
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import config as C

RAW = Path(__file__).resolve().parent.parent / "data" / "raw"
RAW.mkdir(parents=True, exist_ok=True)

# Per-year source record counts = the change token (see module docstring).
COUNTS = RAW / "source_counts.json"

# Floor at the SMD choropleth's start year (2016 — modern years are clean); the
# radar applies its own, later C.START_YEAR when it reads these files.
FETCH_START_YEAR = 2016

_FIELDS = ("ADDDATE,RESOLUTIONDATE,LATITUDE,LONGITUDE,WARD,"
           "SERVICECODE,SERVICECODEDESCRIPTION,SERVICETYPECODEDESCRIPTION")
HEADER = ["date", "resolved", "lat", "lon", "ward", "code", "service", "agency"]


def _get(url, params):
    q = urllib.parse.urlencode(params)
    for attempt in range(6):
        try:
            with urllib.request.urlopen(f"{url}?{q}", timeout=120) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001 - simple retry
            if attempt == 5:
                raise
            print(f"  retry {attempt+1} ({e})", file=sys.stderr)
            time.sleep(3 * (attempt + 1))


def year_layers():
    """Map each year -> layer id by parsing the MapServer's layer list."""
    meta = _get(C.ARCGIS_SERVICE, {"f": "json"})
    out = {}
    for lyr in meta["layers"]:
        name = lyr["name"]  # e.g. "All Service Requests - 2025"
        if name.startswith("All Service Requests - "):
            tail = name.rsplit("-", 1)[-1].strip()
            if tail.isdigit():
                out[int(tail)] = lyr["id"]
    return out


def year_counts(layers, years):
    """{year: total record count} via returnCountOnly — the per-year change token."""
    out = {}
    for y in years:
        d = _get(f"{C.ARCGIS_SERVICE}/{layers[y]}/query",
                 {"where": "1=1", "returnCountOnly": "true", "f": "json"})
        out[y] = d.get("count")
    return out


def load_counts():
    if COUNTS.exists():
        try:
            return json.loads(COUNTS.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def changed_years(layers, years):
    """Years to (re)fetch: the current year always, plus any prior year whose
    source count *changed* since we last stored it (a backfill). A prior year we
    have no stored count for is TRUSTED (reuse its committed checkpoint, just
    record the count) — so a fresh clone never re-pulls all of history. Returns
    (set_of_years, live_counts) so main can persist the fresh counts."""
    live = year_counts(layers, years)
    stored = load_counts()
    current = max(years)
    changed = {current}
    for y in years:
        sy = str(y)
        if sy in stored and stored[sy] != live[y]:
            changed.add(y)   # known count, and it moved -> backfill, re-fetch
    return changed, live


PAGE = 1000  # ArcGIS max rows per query


def _row(a):
    """One ArcGIS attribute dict -> a CSV row in HEADER order. Rows with no date or
    no lat/lon are KEPT (empty fields) so the file's row count == the layer count
    (our change token) and SMD's no-latlon bucket stays honest; build.py skips the
    dateless ones at read time."""
    ms = a.get("ADDDATE")
    d = (datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()
         if ms else "")
    rms = a.get("RESOLUTIONDATE")
    resolved = (datetime.fromtimestamp(rms / 1000, tz=timezone.utc).date().isoformat()
                if rms else "")
    lat, lon = a.get("LATITUDE"), a.get("LONGITUDE")
    return (d, resolved,
            f"{lat:.6f}" if lat is not None else "",
            f"{lon:.6f}" if lon is not None else "",
            a.get("WARD") or "",
            (a.get("SERVICECODE") or "").strip(),
            (a.get("SERVICECODEDESCRIPTION") or "").strip(),
            (a.get("SERVICETYPECODEDESCRIPTION") or "").strip())


def fetch_year_all(layer_id, year):
    """Stream one year's FULL request set to data/raw/_all_<year>.csv, **paging to
    disk** so a kill mid-year resumes from the last complete page instead of losing
    the year. Writes to a `.part` file (appended per page) and atomically renames to
    the final name on completion — so a present final file means "already done", and
    a present `.part` means "resume here". Deterministic `orderByFields=ADDDATE`
    makes offset-based resume safe."""
    final = RAW / f"_all_{year}.csv"
    if final.exists():
        print(f"  {year}: already complete", file=sys.stderr)
        return
    part = RAW / f"_all_{year}.csv.part"
    offset = 0
    if part.exists():
        # resume at the last CLEAN page boundary; drop any partial tail page
        with part.open() as fh:
            lines = fh.readlines()
        offset = (max(0, len(lines) - 1) // PAGE) * PAGE  # -1 for header
        with part.open("w") as fh:
            fh.writelines(lines[:offset + 1])             # header + offset rows
        print(f"  {year}: resuming at offset {offset:,}", file=sys.stderr)
    else:
        with part.open("w", newline="") as fh:
            csv.writer(fh).writerow(HEADER)
    with part.open("a", newline="") as fh:
        w = csv.writer(fh)
        while True:
            data = _get(
                f"{C.ARCGIS_SERVICE}/{layer_id}/query",
                {"where": "1=1", "outFields": _FIELDS, "returnGeometry": "false",
                 "resultOffset": offset, "resultRecordCount": PAGE,
                 "orderByFields": "ADDDATE", "f": "json"},
            )
            feats = data.get("features", [])
            if not feats:
                break
            w.writerows(_row(f["attributes"]) for f in feats)
            fh.flush()
            offset += len(feats)
            if offset % 20000 == 0:
                print(f"  {year}: {offset:,} rows", file=sys.stderr)
            if len(feats) < PAGE:
                break
    part.replace(final)  # atomic: mark the year complete
    print(f"  {year}: complete ({offset:,} rows)", file=sys.stderr)


def main():
    layers = year_layers()
    years = sorted(y for y in layers if y >= FETCH_START_YEAR)
    refetch, live = changed_years(layers, years)
    # Force a fresh pull for years that changed / the current year: drop their final
    # (and any stale .part) so fetch_year_all re-pulls from scratch rather than
    # short-circuiting on the existing final. Missing/partial years are left as-is
    # so fetch_year_all resumes their .part from the last complete page.
    for y in refetch:
        (RAW / f"_all_{y}.csv").unlink(missing_ok=True)
        (RAW / f"_all_{y}.csv.part").unlink(missing_ok=True)
    todo = [y for y in years if not (RAW / f"_all_{y}.csv").exists()]
    reused = [y for y in years if y not in todo]
    print(f"Fetch ALL 311 records: (re)fetch={sorted(todo) or '—'} · "
          f"reuse committed={reused or '—'}")
    # Parallel across years (network-bound; each thread streams its own file, so
    # there's no shared mutable state). CI normally only has the current year to do.
    if todo:
        with ThreadPoolExecutor(max_workers=6) as ex:
            list(ex.map(lambda y: fetch_year_all(layers[y], y), todo))
    # Persist the fresh per-year counts so the next run can detect changes.
    COUNTS.write_text(json.dumps({str(y): live[y] for y in years},
                                 indent=2, sort_keys=True))
    print(f"Wrote {COUNTS.name} for years {years[0]}-{years[-1]}")


if __name__ == "__main__":
    main()
