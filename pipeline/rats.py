"""Rat-report export for the Home Rule abatement coordinator (abatement.home-rule.org).

The coordinator shows "how bad has it been around here?" after an address lookup:
rat reports within about a block, the trend, how the SMD ranks, and city blocks
shaded by recent reports. It doesn't talk to DC itself — it pulls these files from
this site's Pages deploy on a schedule (a cron in that Worker checks meta.json and
reloads only when `generated` changes). So this is a PUBLISHED CONTRACT: bump
SCHEMA on any breaking change to the shapes below, and the consumer refuses a
schema it doesn't know rather than loading garbage.

Writes agg/rats/:
  meta.json            schema, generated, data_through, totals, file list + sha256
  reports-<year>.json  {"columns": [opened, resolved, lat, lon, smd], "rows": [...]}
  smd.json             per-SMD last12/prev12/total/rank/by_year (windows end at data_through)
  blocks.json          DC Squares (street-bounded blocks) with >=1 report in 24 months:
                       bbox, last12, last24, geometry (5-decimal GeoJSON)

Keyed on service CODE S0311, never the label: DC renamed it mid-2026 ("Rodent
Inspection and Treatment" -> "DC Health Rodent & Vector Control").

Placeholder pins (non-geographic requests dumped on one default coordinate) are
dropped with the same rule as smd.py: a coordinate carrying more than
PLACEHOLDER_MAX_PER_YEAR requests of ANY service in a year. That needs every
service's rows, which is why this lives here and not in the consumer.

Run:  cd pipeline && ./.venv/bin/python rats.py   (after fetch.py)
"""
import csv
import hashlib
import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone

import shapely
from shapely.geometry import Point, mapping, shape

import smd as S

SCHEMA = 1
RAT_CODE = "S0311"
START_YEAR = 2016            # matches smd.py; SMD boundaries are 2023
OUT = S.ROOT / "agg" / "rats"

SQUARE_LAYER = ("https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_DATA/"
                "Property_and_Land_WebMercator/MapServer/41")
# Typical residential squares are 10-60k m^2. Parks, campuses and federal land run to
# millions; shading a whole park for 3 reports would exaggerate, so bigger squares
# still count toward every total but aren't published as shaded blocks.
MAX_SHADED_SQUARE_M2 = 150_000
COORD_DECIMALS = 5           # ~1 m — plenty for block outlines

csv.field_size_limit(10 * 1024 * 1024)


def years_before(iso, n):
    """Same calendar day n years earlier, as a YYYY-MM-DD string (lexical cutoff)."""
    return f"{int(iso[:4]) - n}{iso[4:]}"


def load_reports():
    """Rat rows from every _all_<year>.csv, minus placeholder pins. Returns
    (rows, dropped) where rows are dicts with opened/resolved/lat/lon/year."""
    files = {y: p for y, p in S.all_year_files().items() if y >= START_YEAR}
    if not files:
        raise SystemExit("No data/raw/_all_*.csv — run fetch.py first.")
    rows, dropped = [], 0
    for y in sorted(files):
        coord_counts, rats = Counter(), []
        with files[y].open() as fh:
            for r in csv.DictReader(fh):
                lat, lon = r.get("lat"), r.get("lon")
                if not lat or not lon:
                    continue
                coord_counts[(lat, lon)] += 1
                if r.get("code") == RAT_CODE:
                    rats.append(r)
        placeholders = {c for c, k in coord_counts.items() if k > S.PLACEHOLDER_MAX_PER_YEAR}
        for r in rats:
            if (r["lat"], r["lon"]) in placeholders:
                dropped += 1
                continue
            opened = (r.get("date") or "")[:10]
            if len(opened) != 10:
                continue
            resolved = (r.get("resolved") or "")[:10]
            # File each report under the year it was OPENED, not the source layer:
            # DC's per-year layers carry a few next-year Jan 1 requests, and the
            # consumer replaces one calendar year (by opened date) at a time.
            rows.append({"opened": opened, "resolved": resolved if len(resolved) == 10 else None,
                         "lat": float(r["lat"]), "lon": float(r["lon"]), "year": int(opened[:4])})
        print(f"  {y}: {len(rats):,} rat rows · {len(placeholders)} placeholder pins",
              file=sys.stderr)
    return rows, dropped


def fetch_squares():
    """Every live DC Square (KILL_DT IS NULL — retired squares overlap their
    replacements) as (id, shapely geometry, area_m2)."""
    feats, offset = [], 0
    while True:
        gj = S._get(f"{SQUARE_LAYER}/query", {
            "where": "KILL_DT IS NULL",
            "outFields": "SQUARE,SUFFIX",
            "returnGeometry": "true",
            "outSR": 4326,
            "geometryPrecision": 6,
            "resultOffset": offset,
            "resultRecordCount": 2000,
            "f": "geojson",
        })
        batch = gj.get("features") or []
        feats.extend(batch)
        if len(batch) < 2000:
            break
        offset += 2000
    out = []
    for f in feats:
        g = f.get("geometry")
        if not g or g.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        p = f.get("properties") or {}
        sid = f"{(p.get('SQUARE') or '').strip()}{(p.get('SUFFIX') or '').strip()}"
        if not sid:
            continue
        geom = shape(g)
        # Equirectangular m^2 — fine at block scale and DC's latitude.
        lat = geom.centroid.y
        m2 = geom.area * (111_320 ** 2) * math.cos(math.radians(lat))
        out.append((sid, geom, m2))
    return out


def write_json(name, obj):
    """Write compact JSON, return its manifest entry (path + sha256 + bytes)."""
    data = json.dumps(obj, separators=(",", ":")).encode()
    (OUT / name).write_bytes(data)
    return {"path": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.json"):   # a dropped year must not linger as a stale file
        old.unlink()

    rows, dropped = load_reports()
    data_through = max(r["opened"] for r in rows)
    cut12, cut24 = years_before(data_through, 1), years_before(data_through, 2)

    # --- SMD assignment + rollup (same polygons + test as smd.py)
    smd_ids, geoms, tree, _labels, _src = S.load_smd_polygons()
    by_coord = {}
    for r in rows:
        k = (r["lat"], r["lon"])
        if k not in by_coord:
            i = S.assign(r["lat"], r["lon"], geoms, tree)
            by_coord[k] = smd_ids[i] if i is not None else None
        r["smd"] = by_coord[k]
    stats = {sid: {"last12": 0, "prev12": 0, "total": 0, "by_year": Counter()} for sid in smd_ids}
    for r in rows:
        s = stats.get(r["smd"])
        if not s:
            continue
        s["total"] += 1
        s["by_year"][str(r["year"])] += 1
        if r["opened"] > cut12:
            s["last12"] += 1
        elif r["opened"] > cut24:
            s["prev12"] += 1
    ordered = sorted(stats.items(), key=lambda kv: -kv[1]["last12"])
    rank, prev = 0, None
    for i, (_sid, s) in enumerate(ordered):
        if s["last12"] != prev:
            rank, prev = i + 1, s["last12"]
        s["rank"] = rank
    l12 = sorted(s["last12"] for s in stats.values())
    median_smd = l12[len(l12) // 2] if l12 else 0

    # --- Block (DC Square) shading over the last 24 months
    squares = fetch_squares()
    sq_geoms = [g for _, g, _ in squares]
    sq_tree = shapely.STRtree(sq_geoms)
    block = {}   # id -> {"geom", "m2", "last12", "last24"}  (split squares merge by id)
    recent = inside = 0
    for r in rows:
        if r["opened"] <= cut24:
            continue
        recent += 1
        p = Point(r["lon"], r["lat"])
        hit = next((int(i) for i in sq_tree.query(p) if sq_geoms[i].contains(p)), None)
        if hit is None:
            continue
        inside += 1
        sid, geom, m2 = squares[hit]
        b = block.setdefault(sid, {"geom": geom, "m2": m2, "last12": 0, "last24": 0})
        b["last24"] += 1
        if r["opened"] > cut12:
            b["last12"] += 1
    block_rows, max_span = [], 0.0
    for sid, b in sorted(block.items()):
        if b["m2"] > MAX_SHADED_SQUARE_M2:
            continue
        x0, y0, x1, y1 = b["geom"].bounds
        max_span = max(max_span, y1 - y0)
        g = mapping(b["geom"])
        g = {"type": g["type"], "coordinates": S._round_coords(g["coordinates"], COORD_DECIMALS)}
        block_rows.append([sid, round(y0, 6), round(y1, 6), round(x0, 6), round(x1, 6),
                           b["last12"], b["last24"], g])

    # --- Write files + manifest (meta.json LAST, so a crash never publishes a
    # manifest pointing at files that weren't written)
    files = {"reports": []}
    for y in sorted({r["year"] for r in rows}):
        yr = [[r["opened"], r["resolved"], r["lat"], r["lon"], r["smd"]] for r in rows if r["year"] == y]
        entry = write_json(f"reports-{y}.json", {"schema": SCHEMA, "year": y,
                                                 "columns": ["opened", "resolved", "lat", "lon", "smd"],
                                                 "rows": yr})
        files["reports"].append({"year": y, "rows": len(yr), **entry})
    smd_rows = [[sid, s["last12"], s["prev12"], s["total"], s["rank"], dict(sorted(s["by_year"].items()))]
                for sid, s in ordered]
    files["smd"] = {"rows": len(smd_rows), **write_json("smd.json", {
        "schema": SCHEMA, "columns": ["smd", "last12", "prev12", "total", "rank", "by_year"],
        "rows": smd_rows})}
    files["blocks"] = {"rows": len(block_rows), **write_json("blocks.json", {
        "schema": SCHEMA,
        "columns": ["square", "min_lat", "max_lat", "min_lon", "max_lon", "last12", "last24", "geometry"],
        "rows": block_rows})}
    meta = {
        "schema": SCHEMA,
        "generated": datetime.now(timezone.utc).isoformat(),
        "data_through": data_through,
        "service_code": RAT_CODE,
        "source": "DC 311 (ArcGIS ServiceRequests, service S0311) via dc311-data",
        "start_year": START_YEAR,
        "smd_count": len(smd_ids),
        "median_smd_last12": median_smd,
        "square_max_lat_span": round(max_span, 5),
        "totals": {"reports": len(rows), "placeholder_dropped": dropped,
                   "recent_reports": recent, "recent_in_block": inside,
                   "blocks": len(block_rows), "squares_fetched": len(squares)},
        "files": files,
    }
    (OUT / "meta.json").write_text(json.dumps(meta, indent=1))
    total_kb = sum(p.stat().st_size for p in OUT.glob("*.json")) / 1024
    print(f"Wrote {OUT} · {total_kb:.0f} KB · {len(rows):,} rat reports through {data_through} · "
          f"{dropped:,} placeholder · {inside:,}/{recent:,} recent in a block · "
          f"{len(block_rows):,} shaded blocks · median SMD last-12 {median_smd}")


if __name__ == "__main__":
    main()
