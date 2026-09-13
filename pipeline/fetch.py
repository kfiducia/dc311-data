"""Pull raw 311 records for every configured signal from DC's ArcGIS API.

Writes one tidy CSV per signal: data/raw/<key>_reports.csv (date, lat, lon,
ward, resolved). Paginates each per-year layer at 1000 rows/request. Idempotent:
per-year checkpoints (`_<key>_<year>.csv`) mean a rerun skips cached years.

Signals (and their aux signals) come from config.SIGNALS — each contributes its
own WHERE clause (a code list or an explicit `where`, e.g. every DMV* code).
"""
import csv
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import config as C

RAW = Path(__file__).resolve().parent.parent / "data" / "raw"
RAW.mkdir(parents=True, exist_ok=True)


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


def fetch_year(layer_id, year, where):
    rows, offset = [], 0
    while True:
        data = _get(
            f"{C.ARCGIS_SERVICE}/{layer_id}/query",
            {
                "where": where,
                "outFields": "ADDDATE,RESOLUTIONDATE,LATITUDE,LONGITUDE,WARD",
                "returnGeometry": "false",
                "resultOffset": offset,
                "resultRecordCount": 1000,
                "orderByFields": "ADDDATE",
                "f": "json",
            },
        )
        feats = data.get("features", [])
        if not feats:
            break
        for f in feats:
            a = f["attributes"]
            ms, lat, lon = a.get("ADDDATE"), a.get("LATITUDE"), a.get("LONGITUDE")
            if ms is None or lat is None or lon is None:
                continue
            d = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date()
            rms = a.get("RESOLUTIONDATE")
            resolved = (datetime.fromtimestamp(rms / 1000, tz=timezone.utc).date()
                        .isoformat() if rms else "")
            rows.append((d.isoformat(), f"{lat:.6f}", f"{lon:.6f}",
                         a.get("WARD") or "", resolved))
        offset += len(feats)
        print(f"  {year}: {offset} rows", file=sys.stderr)
        if len(feats) < 1000:
            break
    return rows


def fetch_signal(key, where, layers, years):
    """Fetch one signal's WHERE across all years -> data/raw/<key>_reports.csv.
    Checkpoints each year so a stall never loses prior work; reruns skip cached."""
    print(f"Fetching {key}  [{where}]  for years {years[0]}-{years[-1]}")
    for y in years:
        part = RAW / f"_{key}_{y}.csv"
        if part.exists():
            print(f"  {y}: cached", file=sys.stderr)
            continue
        rows = fetch_year(layers[y], y, where)
        with part.open("w", newline="") as fh:
            csv.writer(fh).writerows(rows)
        print(f"  {y}: wrote {len(rows):,}", file=sys.stderr)

    all_rows = []
    for y in years:
        all_rows += list(csv.reader((RAW / f"_{key}_{y}.csv").open()))
    all_rows.sort()
    out = RAW / f"{key}_reports.csv"
    with out.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "lat", "lon", "ward", "resolved"])
        w.writerows(all_rows)
    print(f"Wrote {len(all_rows):,} rows -> {out}")


def iter_signals():
    """Every fetchable (key, where) pair: each detect signal plus its aux signals."""
    for sig in C.SIGNALS:
        yield sig["key"], C.signal_where(sig)
        for a in sig.get("aux", []):
            yield a["key"], C.signal_where(a)


def main():
    layers = year_layers()
    years = sorted(y for y in layers if y >= C.START_YEAR)
    for key, where in iter_signals():
        fetch_signal(key, where, layers, years)


if __name__ == "__main__":
    main()
