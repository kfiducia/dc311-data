"""Turn raw reports into hex weekly series + causal spike alerts + abatement.

Reads   data/raw/<signal>_reports.csv   (date, lat, lon, ward, resolved)
Writes  agg/<signal>_alerts.json         (consumed by the dashboard)

Detection unit = H3 res-8 hexes (uniform, ~170 over DC, tiles the city, finer
than DC's 46 neighborhood clusters), each labeled with the *nearest* single DC
neighborhood name so a hotspot reads as one place, not a merged cluster.

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


# ----------------------------- main ----------------------------------------
def main():
    rows = list(csv.DictReader((RAW / f"{C.SIGNAL_KEY}_reports.csv").open()))
    print(f"Loaded {len(rows):,} reports")
    name_pts = load_name_points()

    hex_week = defaultdict(lambda: defaultdict(int))   # reports opened / week
    hex_res = defaultdict(lambda: defaultdict(int))    # tickets closed / week
    heat_week = defaultdict(lambda: defaultdict(int))  # fine res-9 grid
    hex_center, hex_label, heat_center = {}, {}, {}
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

        cell = h3.latlng_to_cell(lat, lon, C.H3_RES)
        if cell not in hex_center:
            clat, clon = h3.cell_to_latlng(cell)
            hex_center[cell] = (round(clat, 5), round(clon, 5))
            hex_label[cell] = nearest_name(clat, clon, name_pts)
        hex_week[cell][wk] += 1

        rv = r.get("resolved") or ""
        if rv:
            try:
                hex_res[cell][week_monday(date.fromisoformat(rv))] += 1
            except ValueError:
                pass

        h9 = h3.latlng_to_cell(lat, lon, C.HEAT_RES)
        if h9 not in heat_center:
            hlat, hlon = h3.cell_to_latlng(h9)
            heat_center[h9] = (round(hlat, 5), round(hlon, 5))
        heat_week[h9][wk] += 1

    weeks = []
    w = min_wk
    while w <= max_wk:
        weeks.append(w)
        w += timedelta(days=7)
    widx = {w: i for i, w in enumerate(weeks)}
    print(f"{len(weeks)} weeks, {min_wk} .. {max_wk}; {len(hex_center)} hexes, "
          f"{len(heat_center)} heat cells")

    def series_for(week_map):
        c = [0] * len(weeks)
        for wk, v in week_map.items():
            if wk in widx:
                c[widx[wk]] += v
        return c

    # auxiliary corroborating signals (e.g. dead-animal pickups): weekly counts
    # per hex + citywide, bucketed into the SAME res-8 grid. Overlaid, not detected.
    aux_hex, aux_city, aux_meta = {}, {}, []
    for a in getattr(C, "AUX_SIGNALS", []):
        path = RAW / f"{a['key']}_reports.csv"
        if not path.exists():
            continue
        by_cell = defaultdict(lambda: defaultdict(int))
        for r in csv.DictReader(path.open()):
            try:
                lat, lon = float(r["lat"]), float(r["lon"])
                d = date.fromisoformat(r["date"])
            except (ValueError, KeyError):
                continue
            if not (38.7 < lat < 39.05 and -77.15 < lon < -76.85):
                continue
            by_cell[h3.latlng_to_cell(lat, lon, C.H3_RES)][week_monday(d)] += 1
        aux_hex[a["key"]] = {cell: series_for(wm) for cell, wm in by_cell.items()}
        city = [0] * len(weeks)
        for s in aux_hex[a["key"]].values():
            for i in range(len(weeks)):
                city[i] += s[i]
        aux_city[a["key"]] = city
        aux_meta.append({"key": a["key"], "label": a["label"], "total": sum(city)})
        print(f"  aux '{a['key']}': {sum(city):,} reports")

    # per-hex units (the detection + map unit)
    units = []
    for cell in hex_center:
        counts = series_for(hex_week[cell])
        if sum(counts) < 40:  # skip near-empty hexes
            continue
        resolved = series_for(hex_res[cell])
        expected, zs, alert = detect(weeks, counts)
        eps = episodes(weeks, counts, expected, alert)
        units.append({
            "id": cell, "label": hex_label.get(cell) or "—",
            "center": list(hex_center[cell]),
            "boundary": [[round(la, 5), round(lo, 5)]
                         for la, lo in h3.cell_to_boundary(cell)],
            "total": sum(counts), "counts": counts,
            "expected": expected, "z": zs,
            "resolved": resolved,
            "alert_weeks": [i for i, a in enumerate(alert) if a],
            "episodes": eps,
            "abatement": abatement(counts, resolved, expected),
            "aux": {k: aux_hex[k].get(cell, [0] * len(weeks)) for k in aux_hex},
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
        "signal": C.SIGNAL_KEY, "signal_label": C.SIGNAL_LABEL,
        "detect_res": C.H3_RES, "heat_res": C.HEAT_RES,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_through": weeks[-1].isoformat() if weeks else None,
        "generated_from": {"start_year": C.START_YEAR, "service_code": C.SERVICE_CODE},
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
    dest = AGG / f"{C.SIGNAL_KEY}_alerts.json"
    dest.write_text(json.dumps(out, separators=(",", ":")))
    total_eps = sum(len(u["episodes"]) for u in units)
    print(f"Wrote {dest}  ({dest.stat().st_size/1024:.0f} KB)")
    print(f"  {len(units)} hex units, {len(heat)} heat cells, "
          f"{total_eps} spike episodes")
    print(f"  citywide abatement: {city_abate}")


if __name__ == "__main__":
    main()
