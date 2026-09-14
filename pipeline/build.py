"""Turn raw reports into hex weekly series + causal spike alerts + abatement.

Reads   data/raw/_all_<year>.csv         (unified all-records source, DCD10),
        filtered by SERVICECODE per signal (config.resolve_signals)
Writes  agg/<signal>_alerts.json         (one per config.SIGNALS entry)
        agg/signals.json                 (manifest the dashboard's picker reads)

Detection unit = ANC Single Member Districts (SMDs) — DC's ~345 real political
micro-districts. Each report is assigned to its SMD by point-in-polygon (reusing
smd.py's boundary loader + STRtree), and each SMD is labeled "<code> · <nearest
neighborhood>" so a hotspot reads as a recognizable place, not a bare code. The
fine res-9 heat grid is still H3 (a geometry-agnostic density overlay).

Detector = trend-adaptive seasonal aberration detection (syndromic-surveillance
style), scanned causally so alert dates honestly answer "when could we have
known?" Also tracks ticket closures (RESOLUTIONDATE) to gauge whether abatement
is driving reports back down.
"""
import csv
import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import h3

import config as C
import smd  # reuse SMD boundary loader + point-in-polygon (agg/smd_boundaries.*)

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
AGG = ROOT / "agg"
AGG.mkdir(exist_ok=True)


# ----------------------------- labels --------------------------------------
def load_name_points():
    """[(name, lat, lon)] from DC's Neighborhood Names label points."""
    gj = json.loads((RAW / "neighborhood_names.geojson").read_text())
    pts = []
    for f in gj["features"]:
        g = f.get("geometry")
        if not g:
            continue
        lon, lat = g["coordinates"][0], g["coordinates"][1]
        pts.append((f["properties"].get("NAME") or "?", lat, lon))
    return pts


def nearest_name(lat, lon, pts):
    best, bd = None, 1e18
    for nm, la, lo in pts:
        d = (la - lat) ** 2 + (lo - lon) ** 2
        if d < bd:
            bd, best = d, nm
    return best


# ----------------------------- time helpers --------------------------------
def week_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def iso_woy(d: date) -> int:
    return d.isocalendar().week


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        return None
    m = n // 2
    return xs[m] if n % 2 else (xs[m - 1] + xs[m]) / 2


# ----------------------------- detector ------------------------------------
def detect(weeks, counts):
    """Causal, trend-adaptive seasonal aberration scan. Returns exp[],z[],alert[]."""
    woys = [iso_woy(w) for w in weeks]
    yrs = [w.isocalendar().year for w in weeks]
    n = len(weeks)
    expected = [None] * n
    zs = [None] * n
    alert = [False] * n

    base = [None] * n
    for i in range(n):
        ref = []
        for k in range(i):
            if yrs[k] >= yrs[i]:
                continue
            dw = abs(woys[k] - woys[i])
            dw = min(dw, 52 - dw)
            if dw <= C.WOY_WINDOW:
                ref.append(counts[k])
        if len(ref) >= C.MIN_REF_POINTS:
            base[i] = sum(ref) / len(ref)

    for i in range(n):
        if base[i] is None or base[i] < 0.5:
            continue
        ratios = [counts[k] / base[k]
                  for k in range(max(0, i - 8), i)
                  if base[k] and base[k] >= 1.0]
        level = _median(ratios) if len(ratios) >= 3 else 1.0
        level = min(max(level, 0.5), 4.0)
        exp = base[i] * level
        var = max(exp * 1.6, 4.0)
        z = (counts[i] - exp) / (var ** 0.5)
        expected[i] = round(exp, 2)
        zs[i] = round(z, 2)
        if counts[i] >= C.MIN_ABS and z >= C.ALERT_Z:
            alert[i] = True

    run = 0
    for i in range(n):
        z = zs[i]
        warm = z is not None and z >= C.PERSIST_Z and counts[i] >= C.MIN_ABS
        run = run + 1 if warm else 0
        if run >= C.PERSIST_WEEKS:
            for k in range(i - run + 1, i + 1):
                alert[k] = True
    return expected, zs, alert


def episodes(weeks, counts, expected, alert):
    """Maximal elevated (obs>expected) runs containing >=1 alert. Records both
    ISO dates and integer week indices (indices make chart zoom robust)."""
    n = len(weeks)
    elevated = [expected[k] is not None and counts[k] > expected[k] for k in range(n)]
    out = []
    i = 0
    while i < n:
        if not elevated[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and (elevated[j + 1] or (j + 2 < n and elevated[j + 2])):
            j += 1
        run = range(i, j + 1)
        alert_idx = [k for k in run if alert[k]]
        if alert_idx:
            start = alert_idx[0]
            peak = max(run, key=lambda k: counts[k])
            out.append({
                "start_i": start, "peak_i": peak,
                "start": weeks[start].isoformat(), "peak": weeks[peak].isoformat(),
                "peak_obs": counts[peak], "expected_at_peak": expected[peak],
                "lead_weeks": max(0, peak - start),
                "alert_obs": counts[start], "alert_expected": expected[start],
            })
        i = j + 1
    return out


# ----------------------------- abatement -----------------------------------
def _pearson(a, b):
    n = len(a)
    if n < 3:
        return 0.0
    ma, mb = sum(a) / n, sum(b) / n
    sa = sum((x - ma) ** 2 for x in a) ** 0.5
    sb = sum((x - mb) ** 2 for x in b) ** 0.5
    if sa == 0 or sb == 0:
        return 0.0
    return sum((a[i] - ma) * (b[i] - mb) for i in range(n)) / (sa * sb)


def abatement(counts, resolved, expected):
    """Does closing tickets drive future reports below the seasonal baseline?

    We correlate this week's ticket closures with the *excess* reports
    (observed - seasonal expected) a few weeks later, **partialling out the
    current excess** so we're not just measuring regression-to-the-mean. A
    negative partial correlation at a positive lag = treatment is followed by
    fewer-than-expected reports (evidence abatement is working). Observational,
    so it's suggestive, not proof.
    """
    n = len(counts)
    x = [(counts[i] - expected[i]) if expected[i] is not None else None
         for i in range(n)]
    best = None
    for L in range(1, 7):
        r, y, cx = [], [], []  # resolutions, future excess, current excess
        for i in range(n - L):
            if x[i] is None or x[i + L] is None:
                continue
            r.append(resolved[i]); y.append(x[i + L]); cx.append(x[i])
        if len(r) < 25:
            continue
        r_ry = _pearson(r, y)
        r_rx = _pearson(r, cx)
        r_xy = _pearson(cx, y)
        denom = ((1 - r_rx ** 2) * (1 - r_xy ** 2)) ** 0.5
        pc = (r_ry - r_rx * r_xy) / denom if denom > 1e-6 else r_ry
        if best is None or pc < best["pcorr"]:
            best = {"lag": L, "pcorr": round(pc, 3), "n": len(r)}
    if not best:
        return None
    pc = best["pcorr"]
    best["verdict"] = ("cooling" if pc <= -0.15
                       else "rising" if pc >= 0.15 else "flat")
    return best


# ----------------------------- per-signal build ----------------------------
def build_signal(sig, smd_ctx, by_code):
    """Build agg/<key>_alerts.json for one signal. `by_code` maps SERVICECODE ->
    list of report rows (from the unified data/raw/_all_<year>.csv, DCD10).
    Returns a manifest entry, or None if the signal has no rows yet."""
    key, label = sig["key"], sig["label"]
    codes = sig.get("codes") or []
    rows = [r for c in codes for r in by_code.get(c, [])]
    print(f"[{key}] {len(rows):,} reports (codes {codes})")
    if not rows:
        print(f"[{key}] empty — skipping")
        return None

    smd_ids, geoms, tree = smd_ctx["ids"], smd_ctx["geoms"], smd_ctx["tree"]
    smd_centers, smd_labels = smd_ctx["centers"], smd_ctx["labels"]

    unit_week = defaultdict(lambda: defaultdict(int))  # reports opened / week / SMD
    unit_res = defaultdict(lambda: defaultdict(int))   # tickets closed / week / SMD
    heat_week = defaultdict(lambda: defaultdict(int))  # fine res-9 H3 grid (overlay)
    heat_center = {}
    min_wk = max_wk = None

    for r in rows:
        try:
            lat, lon = float(r["lat"]), float(r["lon"])
            d = date.fromisoformat(r["date"])
        except (ValueError, KeyError):
            continue
        if not (38.7 < lat < 39.05 and -77.15 < lon < -76.85):
            continue
        wk = week_monday(d)
        min_wk = wk if min_wk is None or wk < min_wk else min_wk
        max_wk = wk if max_wk is None or wk > max_wk else max_wk

        # fine res-9 heat grid (geometry-agnostic density overlay; kept as H3)
        h9 = h3.latlng_to_cell(lat, lon, C.HEAT_RES)
        if h9 not in heat_center:
            hlat, hlon = h3.cell_to_latlng(h9)
            heat_center[h9] = (round(hlat, 5), round(hlon, 5))
        heat_week[h9][wk] += 1

        # detection/display unit = the SMD that contains the point (point-in-polygon)
        idx = smd.assign(lat, lon, geoms, tree)
        if idx is None:                # river / boundary gap / bad geocode -> no SMD
            continue
        sid = smd_ids[idx]
        unit_week[sid][wk] += 1

        rv = r.get("resolved") or ""
        if rv:
            try:
                unit_res[sid][week_monday(date.fromisoformat(rv))] += 1
            except ValueError:
                pass

    if min_wk is None:
        print(f"[{key}] no in-DC points — skipping")
        return None

    weeks = []
    w = min_wk
    while w <= max_wk:
        weeks.append(w)
        w += timedelta(days=7)
    widx = {w: i for i, w in enumerate(weeks)}
    print(f"[{key}] {len(weeks)} weeks, {min_wk} .. {max_wk}; {len(unit_week)} SMDs, "
          f"{len(heat_center)} heat cells")

    def series_for(week_map):
        c = [0] * len(weeks)
        for wk, v in week_map.items():
            if wk in widx:
                c[widx[wk]] += v
        return c

    # auxiliary corroborating signals (e.g. dead-animal pickups): weekly counts
    # per SMD + citywide, assigned by the SAME point-in-polygon. Overlaid, not detected.
    aux_unit, aux_city, aux_meta = {}, {}, []
    for a in sig.get("aux", []):
        arows = [r for c in (a.get("codes") or []) for r in by_code.get(c, [])]
        if not arows:
            continue
        by_unit = defaultdict(lambda: defaultdict(int))
        for r in arows:
            try:
                lat, lon = float(r["lat"]), float(r["lon"])
                d = date.fromisoformat(r["date"])
            except (ValueError, KeyError):
                continue
            if not (38.7 < lat < 39.05 and -77.15 < lon < -76.85):
                continue
            idx = smd.assign(lat, lon, geoms, tree)
            if idx is None:
                continue
            by_unit[smd_ids[idx]][week_monday(d)] += 1
        aux_unit[a["key"]] = {sid: series_for(wm) for sid, wm in by_unit.items()}
        city = [0] * len(weeks)
        for s in aux_unit[a["key"]].values():
            for i in range(len(weeks)):
                city[i] += s[i]
        aux_city[a["key"]] = city
        aux_meta.append({"key": a["key"], "label": a["label"], "total": sum(city)})
        print(f"  aux '{a['key']}': {sum(city):,} reports")

    # per-SMD units (the detection + map unit). Geometry is NOT embedded — the
    # front-end joins these to agg/smd_boundaries.min.geojson by SMD id.
    units = []
    for sid in unit_week:
        counts = series_for(unit_week[sid])
        if sum(counts) < 40:  # skip near-empty SMDs
            continue
        resolved = series_for(unit_res[sid])
        expected, zs, alert = detect(weeks, counts)
        eps = episodes(weeks, counts, expected, alert)
        units.append({
            "id": sid, "label": smd_labels.get(sid) or sid,
            "center": list(smd_centers.get(sid, (None, None))),
            "total": sum(counts), "counts": counts,
            "expected": expected, "z": zs,
            "resolved": resolved,
            "alert_weeks": [i for i, a in enumerate(alert) if a],
            "episodes": eps,
            "abatement": abatement(counts, resolved, expected),
            "aux": {k: aux_unit[k].get(sid, [0] * len(weeks)) for k in aux_unit},
        })

    # citywide roll-up (for the abatement headline + context)
    city_counts = [sum(u["counts"][i] for u in units) for i in range(len(weeks))]
    city_res = [sum(u["resolved"][i] for u in units) for i in range(len(weeks))]
    city_exp, _, _ = detect(weeks, city_counts)
    city_abate = abatement(city_counts, city_res, city_exp)

    # ticket close-speed stats (part of the "reporting works" story)
    lags = []
    for r in rows:
        rv = r.get("resolved") or ""
        if not rv:
            continue
        try:
            lag = (date.fromisoformat(rv) - date.fromisoformat(r["date"])).days
        except ValueError:
            continue
        if 0 <= lag < 365:
            lags.append(lag)
    close = None
    if lags:
        lags.sort()
        close = {"median_days": lags[len(lags) // 2],
                 "pct_7d": round(100 * sum(1 for l in lags if l <= 7) / len(lags)),
                 "resolved_share": round(100 * len(lags) / len(rows))}

    # fine heat grid
    heat = []
    for cell, ctr in heat_center.items():
        c = series_for(heat_week[cell])
        if sum(c) >= C.HEAT_MIN_TOTAL:
            heat.append({"c": [ctr[0], ctr[1]], "counts": c})

    out = {
        "signal": key, "signal_label": label,
        "detect_unit": "smd", "heat_res": C.HEAT_RES,
        "boundary_file": "agg/smd_boundaries.min.geojson",
        "boundary_source": smd_ctx.get("source"),
        "n_smd": len(units),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_through": weeks[-1].isoformat() if weeks else None,
        "generated_from": {"start_year": C.START_YEAR, "where": C.signal_where(sig)},
        "week_start": [w.isoformat() for w in weeks],
        "detector": {"alert_z": C.ALERT_Z, "persist_z": C.PERSIST_Z,
                     "persist_weeks": C.PERSIST_WEEKS},
        "city": {"counts": city_counts, "resolved": city_res,
                 "expected": city_exp, "abatement": city_abate, "close": close,
                 "aux": aux_city},
        "aux_signals": aux_meta,
        "units": units,
        "heat": heat,
    }
    dest = AGG / f"{key}_alerts.json"
    dest.write_text(json.dumps(out, separators=(",", ":")))
    total_eps = sum(len(u["episodes"]) for u in units)
    print(f"[{key}] wrote {dest.name}  ({dest.stat().st_size/1024:.0f} KB) — "
          f"{len(units)} SMD units, {len(heat)} heat cells, {total_eps} episodes")
    return {
        "key": key, "label": label, "file": f"agg/{key}_alerts.json",
        "data_through": out["data_through"], "total": sum(city_counts),
        "units": len(units),
    }


# ----------------------------- SMD context ---------------------------------
def load_smd_context(name_pts):
    """SMD polygons (via smd.py) + a display label per SMD: "<code> · <nearest
    neighborhood>", so a hotspot reads as a recognizable place, not a bare code."""
    smd_ids, geoms, tree, _smd_names, source_meta = smd.load_smd_polygons()
    centers, labels = {}, {}
    for i, sid in enumerate(smd_ids):
        rp = geoms[i].representative_point()   # guaranteed inside the polygon
        clat, clon = round(rp.y, 5), round(rp.x, 5)
        centers[sid] = (clat, clon)
        nbhd = nearest_name(clat, clon, name_pts)
        labels[sid] = f"{sid} · {nbhd}" if nbhd and nbhd != "?" else sid
    print(f"SMD context: {len(smd_ids)} districts labeled by nearest neighborhood")
    return {"ids": smd_ids, "geoms": geoms, "tree": tree,
            "centers": centers, "labels": labels, "source": source_meta}


# ----------------------------- unified raw source --------------------------
def collect_rows_by_code(codes_needed, start_year):
    """Single pass over data/raw/_all_<year>.csv (year >= start_year), collecting
    each needed SERVICECODE's rows (DCD10 — one read serves every signal + aux,
    instead of a per-signal CSV). Returns {code: [row dicts]}."""
    by_code = {c: [] for c in codes_needed}
    files = sorted(RAW.glob("_all_*.csv"))
    if not files:
        raise SystemExit("No data/raw/_all_*.csv — run fetch.py first.")
    scanned = 0
    for f in files:
        try:
            y = int(f.stem.rsplit("_", 1)[-1])
        except ValueError:
            continue
        if y < start_year:
            continue
        with f.open() as fh:
            for r in csv.DictReader(fh):
                scanned += 1
                bucket = by_code.get(r.get("code"))
                if bucket is not None:
                    bucket.append(r)
    print(f"Scanned {scanned:,} rows from {len(files)} _all_*.csv; "
          f"kept {sum(len(v) for v in by_code.values()):,} across {len(codes_needed)} codes")
    return by_code


# ----------------------------- main ----------------------------------------
def main():
    name_pts = load_name_points()
    smd_ctx = load_smd_context(name_pts)
    signals = C.resolve_signals()
    needed = set()
    for sig in signals:
        needed |= set(sig.get("codes") or [])
        for a in sig.get("aux", []):
            needed |= set(a.get("codes") or [])
    by_code = collect_rows_by_code(needed, C.START_YEAR)
    manifest = []
    for sig in signals:
        entry = build_signal(sig, smd_ctx, by_code)
        if entry:
            manifest.append(entry)
    (AGG / "signals.json").write_text(json.dumps(
        {"generated_at": datetime.now(timezone.utc).isoformat(), "signals": manifest},
        separators=(",", ":")))
    print(f"Wrote agg/signals.json — {len(manifest)} signal(s): "
          f"{', '.join(m['key'] for m in manifest)}")


if __name__ == "__main__":
    main()
