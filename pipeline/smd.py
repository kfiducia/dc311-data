"""Aggregate DC 311 requests by SMD (ANC Single Member District).

The 311 ArcGIS feed carries WARD but *no* SMD/ANC field, so each request is
associated with its SMD by point-in-polygon of its LAT/LON against the 2023 SMD
boundaries (shapely STRtree R-tree prefilter → exact `.contains` test). This is
build-time only: nothing but precomputed counts ships to the browser.

Writes agg/smd.json — index-aligned integer arrays of requests per SMD, sliced
by year and by service-type category (SERVICETYPECODEDESCRIPTION, top-20 + Other),
plus an explicit "unmapped" bucket (no lat/lon · outside any SMD) so no request
is silently dropped.

Mirrors the idioms in refresh_submission.py (_get 6× retry, year_layers) and the
per-year CSV-checkpoint pattern in fetch.py. Rebuilds are cheap: prior years are
immutable and reused from their cached CSV forever; the current year is re-pulled
only when a fetch manifest shows its cache is older than CURRENT_YEAR_MAX_AGE_DAYS
(DC backfills monthly at most). Delete data/raw/_smd_pts_<year>.csv (or the
manifest entry) to force a re-pull. Safe to re-run.

Run:  cd pipeline && ./.venv/bin/python smd.py
"""
import csv
import json
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

import shapely
from shapely.geometry import Point, shape

import config as C

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
RAW.mkdir(parents=True, exist_ok=True)

SMD_LAYER = ("https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_DATA/"
             "Administrative_Other_Boundaries_WebMercator/MapServer/55")
SMD_START_YEAR = 2016   # SMDs are 2023 boundaries; modern years are clean & bounded
TOP_TYPES = 20          # top service categories kept; rest -> "Other"

# Freshness policy for the per-year point checkpoints (data/raw/_smd_pts_<year>.csv):
# prior years are immutable (DC's historical 311 doesn't change) so their cache is
# reused forever; the current year is re-pulled only when its cached copy is older
# than this many days, since DC backfills monthly at most — a full re-pull on every
# rebuild is wasteful. A fetch manifest records when each year was last pulled, so
# this survives cache restores (file mtime is reset by CI cache/checkout and can't
# be trusted).
CURRENT_YEAR_MAX_AGE_DAYS = 20
MANIFEST = RAW / "smd_fetch_manifest.json"

# csv default field-size limit is too small for the occasional long attribute
csv.field_size_limit(10 * 1024 * 1024)


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


def load_manifest():
    """{year_str: fetched_iso_date} — when each year's checkpoint was last pulled."""
    if MANIFEST.exists():
        try:
            return json.loads(MANIFEST.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_manifest(m):
    MANIFEST.write_text(json.dumps(m, indent=2, sort_keys=True))


def load_smd_polygons():
    """Fetch (once, cached) the 2023 SMD boundaries as GeoJSON in WGS84 and build
    a shapely STRtree. Returns (smd_ids, geoms, tree, source_meta)."""
    cache = RAW / "smd_boundaries.geojson"
    if cache.exists():
        gj = json.loads(cache.read_text())
        print(f"SMD boundaries: cached ({len(gj['features'])} features)", file=sys.stderr)
    else:
        gj = _get(f"{SMD_LAYER}/query", {
            "where": "1=1",
            "outFields": "SMD_ID,ANC_ID,NAME",
            "returnGeometry": "true",
            "outSR": 4326,
            "f": "geojson",
        })
        cache.write_text(json.dumps(gj, separators=(",", ":")))
        print(f"SMD boundaries: fetched {len(gj['features'])} features -> {cache}",
              file=sys.stderr)
    smd_ids, geoms, labels = [], [], {}
    for f in gj["features"]:
        p = f["properties"]
        sid = p["SMD_ID"]
        smd_ids.append(sid)
        geoms.append(shape(f["geometry"]))
        labels[sid] = p.get("NAME") or sid
    tree = shapely.STRtree(geoms)
    source_meta = {
        "url": SMD_LAYER,
        "layer": "Single Member District - 2023",
        "id_field": "SMD_ID",
        "n": len(smd_ids),
        "fetched": date.today().isoformat(),
    }
    return smd_ids, geoms, tree, labels, source_meta


def assign(pt_lat, pt_lon, geoms, tree):
    """Integer index of the SMD polygon that CONTAINS the point, else None.

    `tree.query(p)` returns bbox-candidate indices (shapely 2.x); we do the exact
    `.contains` test in Python rather than trusting the query predicate direction.
    Points outside DC (river, boundary gaps, bad geocode) match no polygon -> None.
    """
    p = Point(pt_lon, pt_lat)
    for i in tree.query(p):
        if geoms[i].contains(p):
            return int(i)
    return None


def fetch_year_points(layer_id, year):
    """Paginate one year's requests -> data/raw/_smd_pts_<year>.csv (lat,lon,type).

    Always (re-)fetches when called; callers gate WHICH years need pulling (see
    needs_fetch in main). Rows missing lat/lon are KEPT (empty coords) so they can
    be counted as no_latlon, never silently dropped."""
    part = RAW / f"_smd_pts_{year}.csv"
    rows, offset = [], 0
    while True:
        data = _get(
            f"{C.ARCGIS_SERVICE}/{layer_id}/query",
            {"where": "1=1",
             "outFields": "ADDDATE,LATITUDE,LONGITUDE,SERVICETYPECODEDESCRIPTION",
             "returnGeometry": "false",
             "resultOffset": offset, "resultRecordCount": 1000,
             "orderByFields": "ADDDATE", "f": "json"},
        )
        feats = data.get("features", [])
        if not feats:
            break
        for f in feats:
            a = f["attributes"]
            lat, lon = a.get("LATITUDE"), a.get("LONGITUDE")
            t = (a.get("SERVICETYPECODEDESCRIPTION") or "").strip()
            rows.append((f"{lat:.6f}" if lat is not None else "",
                         f"{lon:.6f}" if lon is not None else "", t))
        offset += len(feats)
        if offset % 20000 == 0:
            print(f"  {year}: {offset} rows", file=sys.stderr)
        if len(feats) < 1000:
            break
    with part.open("w", newline="") as fh:
        csv.writer(fh).writerows(rows)
    print(f"  {year}: wrote {len(rows):,}", file=sys.stderr)
    return part


def canon(t):
    """Canonical service-type label; empty -> 'Unknown type' (folded into Other)."""
    t = (t or "").strip()
    return t if t else "Unknown type"


def main():
    layers = year_layers()
    years = sorted(y for y in layers if y >= SMD_START_YEAR)
    current = max(layers)

    # Decide which years actually need a (re-)pull, so rebuilds don't re-fetch
    # immutable history. Prior years: reuse the cached CSV if present. Current year:
    # reuse unless its manifest age exceeds CURRENT_YEAR_MAX_AGE_DAYS.
    manifest = load_manifest()
    today = date.today()

    def needs_fetch(y):
        part = RAW / f"_smd_pts_{y}.csv"
        if not part.exists():
            return True                       # no cache -> must fetch
        if y != current:
            return False                      # prior year -> immutable, reuse
        fetched = manifest.get(str(y))        # current year -> age-gate
        if not fetched:
            return True
        return (today - date.fromisoformat(fetched)).days >= CURRENT_YEAR_MAX_AGE_DAYS

    to_fetch = [y for y in years if needs_fetch(y)]
    reused = [y for y in years if y not in to_fetch]
    print(f"SMD build: years {years[0]}-{years[-1]} · "
          f"fetch={to_fetch or '—'} · reuse cached={reused or '—'}", file=sys.stderr)

    smd_ids, geoms, tree, labels, source_meta = load_smd_polygons()
    n_smd = len(smd_ids)

    # fetch only the years that need it — parallel (network-bound; each writes its
    # own CSV, so threads never touch shared state). Manifest updated once, after.
    if to_fetch:
        with ThreadPoolExecutor(max_workers=6) as ex:
            list(ex.map(lambda y: fetch_year_points(layers[y], y), to_fetch))
        for y in to_fetch:
            manifest[str(y)] = today.isoformat()
        save_manifest(manifest)

    parts = {y: RAW / f"_smd_pts_{y}.csv" for y in years}

    # first pass: global service-type totals -> top-20 kept, rest -> "Other"
    type_totals = Counter()
    for y in years:
        with parts[y].open() as fh:
            for _, _, t in csv.reader(fh):
                type_totals[canon(t)] += 1
    top = [t for t, _ in type_totals.most_common() if t != "Unknown type"][:TOP_TYPES]
    top_set = set(top)
    service_types = top + ["Other"]   # desc by volume, Other last

    def map_type(t):
        c = canon(t)
        return c if c in top_set else "Other"

    # aggregate: counts[year][type][smd_idx]  (+ "__all__");  unmapped[year][type]
    all_types = ["__all__"] + service_types
    counts = {str(y): {t: [0] * n_smd for t in all_types} for y in years}
    unmapped = {str(y): {t: {"no_latlon": 0, "outside_smd": 0} for t in all_types}
                for y in years}

    for y in years:
        ys = str(y)
        cy, uy = counts[ys], unmapped[ys]
        n = 0
        with parts[y].open() as fh:
            for lat, lon, raw in csv.reader(fh):
                n += 1
                t = map_type(raw)
                if not lat or not lon:
                    uy["__all__"]["no_latlon"] += 1
                    uy[t]["no_latlon"] += 1
                    continue
                idx = assign(float(lat), float(lon), geoms, tree)
                if idx is None:
                    uy["__all__"]["outside_smd"] += 1
                    uy[t]["outside_smd"] += 1
                else:
                    cy["__all__"][idx] += 1
                    cy[t][idx] += 1
        mapped = sum(cy["__all__"])
        um = uy["__all__"]
        print(f"  {y}: {n:,} rows · {mapped:,} mapped · "
              f"{um['no_latlon']:,} no-latlon · {um['outside_smd']:,} outside", file=sys.stderr)

    source_meta["n"] = n_smd
    out = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "boundary_source": source_meta,
        "method": ("point-in-polygon (shapely STRtree) of 311 LAT/LON against "
                   "2023 SMD boundaries"),
        "smds": smd_ids,
        "smd_labels": labels,
        "years": years,
        "service_types": service_types,
        "counts": counts,
        "unmapped": unmapped,
    }
    outp = ROOT / "agg" / "smd.json"
    outp.parent.mkdir(exist_ok=True)
    outp.write_text(json.dumps(out, separators=(",", ":")))
    kb = outp.stat().st_size / 1024
    grand = sum(sum(counts[str(y)]["__all__"]) for y in years)
    print(f"Wrote {outp} · {kb:.0f} KB · {n_smd} SMDs · {len(years)} years · "
          f"{len(service_types)} service types · {grand:,} mapped requests")


if __name__ == "__main__":
    main()
