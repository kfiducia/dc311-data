"""Monthly trend cubes for area-trends.html: one Ward / ANC / SMD at a time.

An ANC commissioner wants to show "trash collection is failing in my SMD" with a
chart: monthly reports for a few complaint types in one area, against the ward
and the city, plus how long DC took to close them. smd.json can't do that (it is
per-year, SMD-only), categories.json is per-ward per-year, and the Complaint Radar
is weekly, one SMD and one signal at a time. This builds, for every area,

    counts[category][month]          requests opened that month
    median_days[category][month]     median days opened -> closed (ANC/ward/city only)

and writes one small file per area, so the page loads only the area it shows plus
its parents (ANC, ward, city) instead of the whole cube.

Geography is FIXED at the 2023 boundaries for every year: each request is placed
in an SMD by point-in-polygon (same polygons and placeholder-pin rule as smd.py),
and its ANC and ward come from that SMD, not from the request's own WARD field.
So an SMD always adds up into its ANC and ward, and a trend line isn't bent by the
2022 ward redistricting. "Citywide" is the sum of all SMDs.

An SMD's ward is the most common WARD field among its requests opened since
WARD_FIELD_FROM, not the ANC id's digit. Two ANCs span two wards: 3/4G (Wards 3
and 4) and 8F (SMDs 8F04/8F05 sit in Ward 6 despite the ANC's "8"). Checked
2026-11-25 against DC GIS "Ward - 2022" (Administrative_Other_Boundaries
MapServer/53) by polygon overlap: all 345 SMDs agree (3/4G01-04 -> Ward 4,
3/4G05-07 -> Ward 3). The window matters: DC's WARD field kept the OLD (pre-2022)
ward lines through 2023 and much of 2024, so counting those years makes some
border SMDs a near-tie (7D06 ~2:1). From 2025-03 every SMD's vote is >=95% one
ward; the build logs any SMD below WARD_PURITY_WARN.

Dates are UTC calendar dates (fetch.py converts DC's epoch timestamps in UTC), so
a request filed late in the evening Eastern can land in the next day / month.

Normalising: DC draws every SMD to about 2,000 residents, so dividing by an area's
SMD count gives a rough per-resident rate without a population table. The page
does that; this file only ships counts and each area's SMD count.

Time to close counts still-open requests as the slowest (not dropped), so recent
months aren't flattered by leaving out what DC hasn't closed; a month whose median
lands on a still-open request is null. Not shipped per SMD: a single SMD rarely has
enough requests in a month for a median to mean anything.

Writes agg/area_trends/ (built in CI, not committed — like agg/rats/):
  index.json       months, categories (+ mapped share), presets, area tree
  <area id>.json   {"id", "all": [..], "c": {code: [..]}, "m": {code: [..]}}

Run:  cd pipeline && ../.venv/bin/python area_trends.py   (after fetch.py)
"""
import csv
import json
import re
import shutil
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import numpy as np
import shapely

import config as C
import smd as S

OUT = S.ROOT / "agg" / "area_trends"
OTHER = "__other__"
OPEN_DAYS = 10 ** 6         # sentinel for "not closed yet": sorts after every real duration
# DC's WARD field still used pre-2022 ward lines through 2023 and much of 2024;
# from here on it matches the current lines (every SMD >=95% one ward).
WARD_FIELD_FROM = "2025-03-01"
WARD_PURITY_WARN = 0.80     # log an SMD whose ward vote is less one-sided than this
CLOSE_LEVELS = ("city", "ward", "anc")   # levels that ship time-to-close

csv.field_size_limit(10 * 1024 * 1024)


# --------------------------------------------------------------------------
# Pure helpers (unit-tested in test_area_trends.py)
# --------------------------------------------------------------------------

def month_index(iso, start_year):
    """'2019-03-14' -> months since January of start_year (may be negative)."""
    return (int(iso[:4]) - start_year) * 12 + int(iso[5:7]) - 1


def month_labels(start_year, n):
    return [f"{start_year + i // 12}-{i % 12 + 1:02d}" for i in range(n)]


def slug(s):
    """Filename/URL-safe id. ANC 3/4G spans Wards 3 and 4 and carries both in its
    id ("3/4G", SMD "3/4G01"), which can't be a file name -> "3-4G", "3-4G01"."""
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")


def ward_digit(field):
    """'Ward 5' -> '5'; anything else (blank, 'Null') -> ''."""
    m = re.fullmatch(r"\s*Ward\s*([1-8])\s*", field or "")
    return m.group(1) if m else ""


def default_ward(anc):
    """Ward implied by an ANC id: its leading digit ("1A" -> "1"), or None when
    the id names several wards ("3/4G")."""
    return anc[0] if re.fullmatch(r"[1-8][A-Z]", anc) else None


def majority_ward(smd_idx, wards):
    """{smd index: (ward digit, share)} = the most common non-blank ward among the
    rows placed in each SMD (ties -> lowest ward) and the share of votes it got.
    smd_idx is an int array (-1 = unplaced); wards is a parallel list of ward
    digits ('' = unknown or outside the voting window)."""
    tally = {}
    for i, w in zip(smd_idx.tolist(), wards):
        if i >= 0 and w:
            tally.setdefault(i, Counter())[w] += 1
    out = {}
    for i, c in tally.items():
        w = min(c, key=lambda k: (-c[k], k))
        out[i] = (w, c[w] / sum(c.values()))
    return out


def resolve_wards(smd_ids, anc_of, majority):
    """Each SMD's ward: the majority of its requests' WARD field, falling back to
    the ANC id's digit for an SMD with no recent requests. Returns (ward_of,
    disagreements): SMDs whose ward differs from their ANC id's digit. Printed
    for the log, not fatal: 8F04/8F05 (Ward 6) are real ones. `majority` is
    majority_ward()'s {smd index: (ward, share)}."""
    ward_of, odd = {}, []
    for i, s in enumerate(smd_ids):
        d = default_ward(anc_of[s])
        w = (majority.get(i) or (None,))[0] or d
        if w is None:   # multi-ward ANC with no recent requests: first ward it names
            w = re.match(r"[1-8]", anc_of[s]).group(0)
        if d and w != d:
            odd.append((s, d, w))
        ward_of[s] = w
    return ward_of, odd


def pick_categories(totals, presets, top_n):
    """Codes to keep, largest first: the top_n by volume plus every preset code
    that has any volume. Everything else folds into OTHER."""
    ranked = [c for c, n in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0])) if n > 0]
    keep = ranked[:top_n]
    extra = {c for p in presets for c in p["codes"]} - set(keep)
    keep += [c for c in ranked[top_n:] if c in extra]
    return keep


def count_cube(area, cat, mon, n_area, n_cat, n_mon):
    """Counts as an (n_area, n_cat, n_mon) int array. Inputs are equal-length int
    arrays; every value must already be in range."""
    key = (area.astype(np.int64) * n_cat + cat) * n_mon + mon
    return np.bincount(key, minlength=n_area * n_cat * n_mon).reshape(n_area, n_cat, n_mon)


def median_cube(area, cat, mon, days, n_area, n_cat, n_mon, min_n, open_days=OPEN_DAYS):
    """Median of `days` per (area, cat, month) as a float array, NaN where the
    cell has fewer than min_n requests or where the median lands on a still-open
    request (days == open_days). Vectorised: one sort, no Python loop."""
    out = np.full(n_area * n_cat * n_mon, np.nan)
    if len(days) == 0:
        return out.reshape(n_area, n_cat, n_mon)
    key = (area.astype(np.int64) * n_cat + cat) * n_mon + mon
    order = np.lexsort((days, key))
    sk, sd = key[order], days[order].astype(np.float64)
    uniq, start, cnt = np.unique(sk, return_index=True, return_counts=True)
    hi = sd[start + cnt // 2]
    med = (sd[start + (cnt - 1) // 2] + hi) / 2
    ok = (cnt >= min_n) & (hi < open_days)   # either middle value open -> unknown
    out[uniq[ok]] = med[ok]
    return out.reshape(n_area, n_cat, n_mon)


def assign_points(lats, lons, tree):
    """Index of the polygon in `tree` containing each point, else -1. Vectorised
    STRtree query; NaN coordinates match nothing. If polygons overlap, the
    lowest polygon index wins (deterministic)."""
    out = np.full(len(lats), -1, dtype=np.int32)
    if len(lats) == 0:
        return out
    pts = shapely.points(lons, lats)
    inp, poly = tree.query(pts, predicate="within")
    if len(inp):
        order = np.lexsort((poly, inp))
        inp, poly = inp[order], poly[order]
        first = np.unique(inp, return_index=True)[1]
        out[inp[first]] = poly[first]
    return out


def area_tree(smd_ids, anc_of, ward_of):
    """Areas in display order: city, wards, ANCs, SMDs, with filename-safe ids.

    ward_of maps every SMD to its actual ward digit. An SMD compares against its
    ANC, ITS OWN ward and the city. An ANC normally lies in one ward; one that
    spans two (3/4G) lists every ward it touches in `wards` (the picker shows it
    under each) and compares against the ward holding most of its SMDs.

    Returns (areas, wards, ancs, smd->anc index, smd->ward index); the indices are
    positions in the ward / ANC lists, so the cube for a level is one bincount."""
    ancs = sorted({anc_of[s] for s in smd_ids})
    wards = sorted({ward_of[s] for s in smd_ids})
    anc_pos = {a: i for i, a in enumerate(ancs)}
    ward_pos = {w: i for i, w in enumerate(wards)}
    n_in_anc = Counter(anc_of[s] for s in smd_ids)
    n_in_ward = Counter(ward_of[s] for s in smd_ids)
    anc_wards = {a: Counter() for a in ancs}
    for s in smd_ids:
        anc_wards[anc_of[s]][ward_of[s]] += 1

    areas = [{"id": "city", "name": "All of DC", "level": "city", "n_smd": len(smd_ids)}]
    areas += [{"id": f"ward-{w}", "name": f"Ward {w}", "level": "ward",
               "n_smd": n_in_ward[w]} for w in wards]
    for a in ancs:
        c = anc_wards[a]
        main = min(c, key=lambda w: (-c[w], w))
        areas.append({"id": f"anc-{slug(a)}", "name": f"ANC {a}", "level": "anc",
                      "ward": f"ward-{main}", "wards": [f"ward-{w}" for w in sorted(c)],
                      "n_smd": n_in_anc[a]})
    areas += [{"id": f"smd-{slug(s)}", "name": f"SMD {s}", "level": "smd",
               "anc": f"anc-{slug(anc_of[s])}", "ward": f"ward-{ward_of[s]}", "n_smd": 1}
              for s in sorted(smd_ids)]
    smd_anc = np.array([anc_pos[anc_of[s]] for s in smd_ids], dtype=np.int32)
    smd_ward = np.array([ward_pos[ward_of[s]] for s in smd_ids], dtype=np.int32)
    return areas, wards, ancs, smd_anc, smd_ward


def series(counts_row, med_row=None):
    """One area's per-category month slices -> JSON-ready lists, medians rounded."""
    c = [int(x) for x in counts_row]
    if med_row is None:
        return c, None
    m = [None if np.isnan(x) else (int(x) if float(x).is_integer() else round(float(x), 1))
         for x in med_row]
    return c, m


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------

def is_partial_month(data_through):
    """True unless data_through ('YYYY-MM-DD') is the last day of its month."""
    d = date.fromisoformat(data_through)
    return (d + timedelta(days=1)).month == d.month


def read_rows(files, start_year, today=None):
    """Read every _all_<year>.csv once into flat arrays. Placeholder pins (a
    coordinate carrying > PLACEHOLDER_MAX_PER_YEAR requests in a year, smd.py's
    rule) and rows without coordinates get NaN coordinates so they place nowhere.

    Rows opened after `today` (UTC; default: now) are skipped: one future-dated
    row would otherwise push data_through ahead, add empty months and trip the
    guardrail, blocking the whole weekly deploy."""
    today = (today or datetime.now(timezone.utc).date()).isoformat()
    lat, lon, code, mon, days, ward = [], [], [], [], [], []
    code_ix, code_label = {}, {}
    ordinal = {}
    data_through = ""
    future = 0

    def ordn(iso):
        o = ordinal.get(iso)
        if o is None:
            o = ordinal[iso] = date.fromisoformat(iso).toordinal()
        return o

    for y in sorted(files):
        with files[y].open() as fh:
            rows = list(csv.DictReader(fh))
        coord_counts = Counter((r.get("lat"), r.get("lon")) for r in rows
                               if r.get("lat") and r.get("lon"))
        placeholders = {k for k, n in coord_counts.items() if n > S.PLACEHOLDER_MAX_PER_YEAR}
        kept = 0
        for r in rows:
            opened = (r.get("date") or "")[:10]
            c = r.get("code") or ""
            if len(opened) < 10 or not c:
                continue
            m = month_index(opened, start_year)
            if m < 0:
                continue
            if opened > today:
                future += 1
                continue
            kept += 1
            if opened > data_through:
                data_through = opened
            if (r.get("service") or "").strip():
                code_label[c] = r["service"].strip()   # ascending years: newest label wins
            la, lo = r.get("lat"), r.get("lon")
            if la and lo and (la, lo) not in placeholders:
                lat.append(float(la))
                lon.append(float(lo))
            else:
                lat.append(np.nan)
                lon.append(np.nan)
            code.append(code_ix.setdefault(c, len(code_ix)))
            mon.append(m)
            ward.append(ward_digit(r.get("ward")) if opened >= WARD_FIELD_FROM else "")
            res = (r.get("resolved") or "")[:10]
            days.append(max(0, ordn(res) - ordn(opened)) if len(res) == 10 else OPEN_DAYS)
        print(f"  {y}: {kept:,} rows · {len(placeholders)} placeholder pins", file=sys.stderr)

    if future:
        print(f"  skipped {future:,} row(s) opened after {today} (future-dated)", file=sys.stderr)
    if not data_through:
        raise SystemExit("area_trends: no usable rows")
    codes = [None] * len(code_ix)
    for c, i in code_ix.items():
        codes[i] = c
    return (np.array(lat), np.array(lon), np.array(code, dtype=np.int32),
            np.array(mon, dtype=np.int32), np.array(days, dtype=np.int64), ward,
            codes, code_label, data_through)


def main():
    start = C.AREA_TRENDS_START_YEAR
    files = {y: p for y, p in S.all_year_files().items() if y >= start}
    if not files:
        raise SystemExit("No data/raw/_all_*.csv — run fetch.py first.")

    smd_ids, _geoms, tree, _labels, _meta = S.load_smd_polygons()
    gj = json.loads((S.RAW / "smd_boundaries.geojson").read_text())
    anc_of = {f["properties"]["SMD_ID"]: (f["properties"].get("ANC_ID") or f["properties"]["SMD_ID"][:-2])
              for f in gj["features"]}

    print(f"Area trends: years {min(files)}-{max(files)} · {len(smd_ids)} SMDs", file=sys.stderr)
    lat, lon, code, mon, days, ward_field, codes, code_label, data_through = read_rows(files, start)
    smd = assign_points(lat, lon, tree)
    print(f"  placed {(smd >= 0).sum():,} of {len(smd):,} requests in an SMD", file=sys.stderr)

    votes = majority_ward(smd, ward_field)
    del ward_field
    ward_of, odd = resolve_wards(smd_ids, anc_of, votes)
    weak = sorted((share, smd_ids[i]) for i, (_w, share) in votes.items() if share < WARD_PURITY_WARN)
    for share, s in weak:
        print(f"  WARNING: SMD {s} ward vote only {share:.0%} for Ward {ward_of[s]} "
              f"(requests since {WARD_FIELD_FROM}) — check it against the ward map", file=sys.stderr)
    shares = [share for _w, share in votes.values()]
    print(f"  ward vote: min purity {min(shares):.1%} over {len(votes)} SMDs "
          f"(requests since {WARD_FIELD_FROM})", file=sys.stderr)
    for s, d, w in odd:
        print(f"  note: SMD {s} is in Ward {w} (its ANC id starts with {d})",
              file=sys.stderr)
    multi = sorted(s for s in smd_ids if default_ward(anc_of[s]) is None)
    if multi:
        print("  multi-ward ANC SMDs: " + ", ".join(f"{s}->Ward {ward_of[s]}" for s in multi),
              file=sys.stderr)
    areas, wards, ancs, smd_anc, smd_ward = area_tree(smd_ids, anc_of, ward_of)

    # Month axis ends at data_through's month (future-dated rows were skipped in
    # read_rows, so this is today's month at the latest).
    n_mon = month_index(data_through, start) + 1
    in_range = mon < n_mon
    all_totals = np.bincount(code[in_range], minlength=len(codes))
    sel = (smd >= 0) & in_range
    smd, code, mon, days = smd[sel], code[sel], mon[sel], days[sel]

    map_totals = np.bincount(code, minlength=len(codes))
    keep = pick_categories({codes[i]: int(n) for i, n in enumerate(map_totals)},
                           C.AREA_TRENDS_PRESETS, C.AREA_TRENDS_TOP_N)
    cat_of_code = np.full(len(codes), len(keep), dtype=np.int32)   # default: OTHER
    for k, c in enumerate(keep):
        cat_of_code[codes.index(c)] = k
    cat = cat_of_code[code]
    cat_keys = keep + [OTHER]
    n_cat = len(cat_keys)

    levels = [
        ("city", np.zeros(len(smd), dtype=np.int32), ["city"]),
        ("ward", smd_ward[smd], [f"ward-{w}" for w in wards]),
        ("anc", smd_anc[smd], [f"anc-{slug(a)}" for a in ancs]),
        ("smd", smd, [f"smd-{slug(s)}" for s in smd_ids]),
    ]

    if OUT.exists():
        shutil.rmtree(OUT)   # generated output only; rebuilt from data/raw every run
    OUT.mkdir(parents=True)
    total_bytes, biggest = 0, (0, "")
    for lvl, area, ids in levels:
        cnt = count_cube(area, cat, mon, len(ids), n_cat, n_mon)
        med = (median_cube(area, cat, mon, days, len(ids), n_cat, n_mon, C.AREA_TRENDS_MIN_CLOSE_N)
               if lvl in CLOSE_LEVELS else None)
        for a, aid in enumerate(ids):
            doc = {"id": aid, "all": [int(x) for x in cnt[a].sum(axis=0)], "c": {}}
            if med is not None:
                doc["m"] = {}
            for k, ck in enumerate(cat_keys):
                if not cnt[a, k].any():
                    continue   # omitted = all zero; the page treats a missing code as zeros
                c, m = series(cnt[a, k], None if med is None else med[a, k])
                doc["c"][ck] = c
                if m is not None:
                    doc["m"][ck] = m
            p = OUT / f"{aid}.json"
            p.write_text(json.dumps(doc, separators=(",", ":")))
            sz = p.stat().st_size
            total_bytes += sz
            biggest = max(biggest, (sz, aid))

    categories = []
    for k, ck in enumerate(cat_keys):
        if ck == OTHER:
            n_map = int((cat == k).sum())
            n_all = int(sum(all_totals[i] for i, c in enumerate(codes) if c not in keep))
            label = "Other categories"
        else:
            i = codes.index(ck)
            n_map, n_all, label = int(map_totals[i]), int(all_totals[i]), code_label.get(ck, ck)
        categories.append({"code": ck, "label": label, "total": n_map,
                           "mapped_pct": round(100 * n_map / n_all, 1) if n_all else None})

    index = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "data_through": data_through,
        "months": month_labels(start, n_mon),
        # the newest month is incomplete unless the data runs to its last day
        "partial_last": is_partial_month(data_through),
        "min_close_n": C.AREA_TRENDS_MIN_CLOSE_N,
        # SMDs whose ward vote was weak (guardrail warns; see WARD_FIELD_FROM)
        "ward_vote": {"since": WARD_FIELD_FROM, "min_share": round(min(shares), 3),
                      "weak": [[s, round(share, 3)] for share, s in weak]},
        "close_levels": list(CLOSE_LEVELS),
        "residents_per_smd": 2000,
        "categories": categories,
        "presets": [p for p in C.AREA_TRENDS_PRESETS if any(c in keep for c in p["codes"])],
        "areas": areas,
        "method": ("Requests placed in a 2023 SMD by point-in-polygon of their LAT/LON "
                   "(as smd.py; placeholder pins excluded). ANC and ward come from the SMD; "
                   "citywide = all SMDs. Months are by the request's opened date in UTC. "
                   "Time to close = resolved minus opened date, by month opened; still-open "
                   "requests count as slowest."),
    }
    ip = OUT / "index.json"
    ip.write_text(json.dumps(index, separators=(",", ":")))
    print(f"Wrote {OUT}/ · {len(areas)} area files · {total_bytes / 1024:.0f} KB total · "
          f"largest {biggest[1]} {biggest[0] / 1024:.1f} KB · index {ip.stat().st_size / 1024:.1f} KB · "
          f"{n_cat} categories · {n_mon} months through {data_through}")


if __name__ == "__main__":
    main()
