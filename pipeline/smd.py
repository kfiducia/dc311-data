"""Aggregate DC 311 requests by SMD (ANC Single Member District).

The 311 ArcGIS feed carries WARD but *no* SMD/ANC field, so each request is
associated with its SMD by point-in-polygon of its LAT/LON against the 2023 SMD
boundaries (shapely STRtree R-tree prefilter → exact `.contains` test). This is
build-time only: nothing but precomputed counts ships to the browser.

Writes agg/smd.json — index-aligned integer arrays of requests per SMD, sliced
by year and by GRANULAR service (SERVICECODEDESCRIPTION, top-20 + Other — the
actual service like "Bulk Collection", not the coarse handling agency), plus an
explicit "unmapped" bucket (no lat/lon · outside any SMD) so no request is
silently dropped.

DCD10: reads the unified data/raw/_all_<year>.csv (produced + git-committed by
fetch.py) — it no longer fetches request rows itself, only the SMD boundary
polygons (cached). Rebuilds are cheap and offline; safe to re-run.

Run:  cd pipeline && ./.venv/bin/python smd.py
"""
import csv
import json
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

import shapely
from shapely.geometry import Point, mapping, shape

import config as C

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
RAW.mkdir(parents=True, exist_ok=True)

SMD_LAYER = ("https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_DATA/"
             "Administrative_Other_Boundaries_WebMercator/MapServer/55")
SMD_START_YEAR = 2016   # SMDs are 2023 boundaries; modern years are clean & bounded
TOP_TYPES = 20          # top service categories kept; rest -> "Other"

# DCD11: non-geographic requests (DC Government Information, DMV issues, ...) are
# geocoded to a single default/placeholder coordinate — one pin can carry thousands
# of requests, faking a district hotspot. No real address generates anywhere near
# this many requests in a year, so any coordinate exceeding this per-year count is
# treated as a placeholder: excluded from the map, counted in an explicit bucket.
PLACEHOLDER_MAX_PER_YEAR = 1000

# DCD10: smd.py no longer fetches request rows — it reads the unified
# data/raw/_all_<year>.csv (produced by fetch.py) and groups by the GRANULAR
# service (SERVICECODEDESCRIPTION), not the coarse handling agency. Only the SMD
# boundary polygons are still fetched here (cached).

# The raw 2023 SMD boundaries are ~4 MB — far too heavy to ship to the browser for
# the choropleth (DCD3). Simplify each polygon and drop coordinate precision to get
# a Pages-safe asset (agg/smd_boundaries.min.geojson, a few hundred KB). Tolerance
# is in degrees (~0.0001 deg ≈ 11 m); 5-decimal coords ≈ 1 m — plenty for a
# city-wide choropleth.
BOUNDARY_SIMPLIFY_TOL = 0.0001
BOUNDARY_COORD_PRECISION = 5

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


def _round_coords(obj, nd):
    """Recursively round a GeoJSON coordinate array to nd decimals."""
    if isinstance(obj, (list, tuple)):
        if obj and isinstance(obj[0], (int, float)):
            return [round(c, nd) for c in obj]
        return [_round_coords(x, nd) for x in obj]
    return obj


def write_min_boundaries():
    """Simplified, low-precision SMD polygons for the client choropleth (DCD3).

    Reads the cached raw boundaries (ensured present by load_smd_polygons),
    shapely-simplifies each polygon and rounds coordinates, and writes a compact
    agg/smd_boundaries.min.geojson (props: id, name) small enough to fetch on a
    static Pages site. Idempotent; boundaries are static (2023) so this is cheap."""
    gj = json.loads((RAW / "smd_boundaries.geojson").read_text())
    feats = []
    for f in gj["features"]:
        g = shape(f["geometry"]).simplify(BOUNDARY_SIMPLIFY_TOL, preserve_topology=True)
        geom = mapping(g)
        geom["coordinates"] = _round_coords(geom["coordinates"], BOUNDARY_COORD_PRECISION)
        p = f["properties"]
        feats.append({"type": "Feature",
                      "properties": {"id": p["SMD_ID"], "name": p.get("NAME") or p["SMD_ID"]},
                      "geometry": geom})
    dest = ROOT / "agg" / "smd_boundaries.min.geojson"
    dest.parent.mkdir(exist_ok=True)
    dest.write_text(json.dumps({"type": "FeatureCollection", "features": feats},
                               separators=(",", ":")))
    print(f"Wrote {dest} · {dest.stat().st_size/1024:.0f} KB · {len(feats)} SMD polygons",
          file=sys.stderr)


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


def canon(t):
    """Canonical service label; empty -> 'Unknown type' (folded into Other)."""
    t = (t or "").strip()
    return t if t else "Unknown type"


def all_year_files():
    """{year: path} for data/raw/_all_<year>.csv with year >= SMD_START_YEAR."""
    out = {}
    for f in RAW.glob("_all_*.csv"):
        try:
            y = int(f.stem.rsplit("_", 1)[-1])
        except ValueError:
            continue
        if y >= SMD_START_YEAR:
            out[y] = f
    return out


def main():
    files = all_year_files()   # data/raw/_all_<year>.csv (committed; produced by fetch.py)
    if not files:
        raise SystemExit("No data/raw/_all_*.csv — run fetch.py first.")
    years = sorted(files)

    smd_ids, geoms, tree, labels, source_meta = load_smd_polygons()
    n_smd = len(smd_ids)
    write_min_boundaries()  # compact polygons for the client choropleth (DCD3)
    print(f"SMD build (from _all): years {years[0]}-{years[-1]} · {n_smd} SMDs",
          file=sys.stderr)

    # first pass: global service totals -> top-20 kept, rest -> "Other". Grouped by
    # the GRANULAR service (SERVICECODEDESCRIPTION), not the coarse handling agency.
    type_totals = Counter()
    for y in years:
        with files[y].open() as fh:
            for r in csv.DictReader(fh):
                type_totals[canon(r.get("service"))] += 1
    top = [t for t, _ in type_totals.most_common() if t != "Unknown type"][:TOP_TYPES]
    top_set = set(top)
    service_types = top + ["Other"]   # desc by volume, Other last

    def map_type(t):
        c = canon(t)
        return c if c in top_set else "Other"

    # aggregate: counts[year][type][smd_idx]  (+ "__all__");  unmapped[year][type]
    all_types = ["__all__"] + service_types
    counts = {str(y): {t: [0] * n_smd for t in all_types} for y in years}
    unmapped = {str(y): {t: {"no_latlon": 0, "outside_smd": 0, "placeholder": 0}
                         for t in all_types} for y in years}

    ph_points = {}   # (lat,lon) -> {"count": int, "svc": Counter} across all years
    for y in years:
        ys = str(y)
        cy, uy = counts[ys], unmapped[ys]
        rows = list(csv.DictReader(files[y].open()))
        n = len(rows)
        # Flag placeholder coordinates (a default pin carrying an implausible count
        # of non-geographic requests) so they don't fake a district hotspot.
        coord_counts = Counter((r.get("lat"), r.get("lon")) for r in rows
                               if r.get("lat") and r.get("lon"))
        placeholders = {c for c, k in coord_counts.items()
                        if k > PLACEHOLDER_MAX_PER_YEAR}
        for r in rows:
            t = map_type(r.get("service"))
            lat, lon = r.get("lat"), r.get("lon")
            if not lat or not lon:
                uy["__all__"]["no_latlon"] += 1
                uy[t]["no_latlon"] += 1
            elif (lat, lon) in placeholders:
                uy["__all__"]["placeholder"] += 1
                uy[t]["placeholder"] += 1
                p = ph_points.setdefault((lat, lon), {"count": 0, "svc": Counter()})
                p["count"] += 1
                p["svc"][canon(r.get("service"))] += 1
            else:
                idx = assign(float(lat), float(lon), geoms, tree)
                if idx is None:
                    uy["__all__"]["outside_smd"] += 1
                    uy[t]["outside_smd"] += 1
                else:
                    cy["__all__"][idx] += 1
                    cy[t][idx] += 1
        um = uy["__all__"]
        print(f"  {y}: {n:,} rows · {sum(cy['__all__']):,} mapped · "
              f"{um['no_latlon']:,} no-latlon · {um['outside_smd']:,} outside · "
              f"{um['placeholder']:,} placeholder ({len(placeholders)} pins)",
              file=sys.stderr)

    # The default/placeholder pins we excluded, largest first — so the UI can say
    # exactly what was dropped and why (non-geographic requests defaulted to one
    # address, e.g. the 311 call center). Rounded coords; top service per pin.
    placeholder_points = sorted(
        ({"lat": round(float(la), 6), "lon": round(float(lo), 6),
          "count": v["count"], "top_service": v["svc"].most_common(1)[0][0]}
         for (la, lo), v in ph_points.items()),
        key=lambda p: -p["count"])

    source_meta["n"] = n_smd
    out = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "boundary_source": source_meta,
        "method": ("point-in-polygon (shapely STRtree) of 311 LAT/LON against "
                   "2023 SMD boundaries; non-geographic requests dumped on a "
                   "default coordinate (>%d/yr) are excluded — see placeholder_points"
                   % PLACEHOLDER_MAX_PER_YEAR),
        "smds": smd_ids,
        "smd_labels": labels,
        "years": years,
        "service_types": service_types,
        "counts": counts,
        "unmapped": unmapped,
        "placeholder_points": placeholder_points,
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
