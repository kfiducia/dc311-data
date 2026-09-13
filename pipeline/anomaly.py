"""Per-ward cross-category anomaly board — the article's method, generalized.

CityCast's piece didn't set out to write about dockless bikes; it ran a
technique — "for each neighborhood, which complaint is unusually high vs. its own
normal, ignoring the ever-present trash/parking" — and the stories fell out. This
builds that device over *every* category: for the latest complete month, for each
ward, how far each category is above that ward's same-month baseline in prior
years. Writes agg/anomalies.json (per-ward standouts + a citywide leaderboard),
fetched at runtime by dashboard.html.

Cheap: one server-side group-by request per month (WARD x SERVICECODE), ~80 of
them. Run:  ./.venv/bin/python pipeline/anomaly.py
"""
import json
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import config as C

ROOT = Path(__file__).resolve().parent.parent
WARDS = [f"Ward {i}" for i in range(1, 9)]


def _get(url, params):
    q = urllib.parse.urlencode(params)
    for attempt in range(6):
        try:
            with urllib.request.urlopen(f"{url}?{q}", timeout=120) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            if attempt == 5:
                raise
            print(f"  retry {attempt+1} ({e})", file=sys.stderr)
            time.sleep(3 * (attempt + 1))


def months_since(start_year):
    """[(year, month), …] from Jan start_year through the current month (UTC)."""
    now = datetime.now(timezone.utc)
    out = []
    y, m = start_year, 1
    while (y, m) <= (now.year, now.month):
        out.append((y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def month_bounds(y, m):
    start = f"{y:04d}-{m:02d}-01 00:00:00"
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    end = f"{ny:04d}-{nm:02d}-01 00:00:00"
    return start, end


def labels_map():
    """{code: description} from the latest year layer."""
    lid = C._latest_layer()
    res = _get(f"{C.ARCGIS_SERVICE}/{lid}/query",
               {"where": "1=1", "groupByFieldsForStatistics": "SERVICECODE,SERVICECODEDESCRIPTION",
                "outStatistics": json.dumps([{"statisticType": "count",
                                              "onStatisticField": "SERVICECODE",
                                              "outStatisticFieldName": "CNT"}]), "f": "json"})
    return {f["attributes"]["SERVICECODE"]: f["attributes"].get("SERVICECODEDESCRIPTION")
            for f in res.get("features", []) if f["attributes"].get("SERVICECODE")}


def fetch_month(lid, y, m):
    """{(ward, code): count} for one month."""
    start, end = month_bounds(y, m)
    where = f"ADDDATE >= timestamp '{start}' AND ADDDATE < timestamp '{end}'"
    res = _get(f"{C.ARCGIS_SERVICE}/{lid}/query",
               {"where": where, "groupByFieldsForStatistics": "WARD,SERVICECODE",
                "outStatistics": json.dumps([{"statisticType": "count",
                                              "onStatisticField": "WARD",
                                              "outStatisticFieldName": "CNT"}]), "f": "json"})
    out = {}
    for f in res.get("features", []):
        a = f["attributes"]
        code, n = a.get("SERVICECODE"), a.get("CNT")
        ward = (a.get("WARD") or "").strip()
        if code and n and ward in WARDS:
            out[(ward, code)] = n
    return out


def year_layers():
    meta = _get(C.ARCGIS_SERVICE, {"f": "json"})
    out = {}
    for lyr in meta["layers"]:
        n = lyr["name"]
        if n.startswith("All Service Requests - "):
            t = n.rsplit("-", 1)[-1].strip()
            if t.isdigit():
                out[int(t)] = lyr["id"]
    return out


def main():
    layers = year_layers()
    labels = labels_map()
    exclude = set(C.EXCLUDE_CODES)

    # month -> {(ward, code): count}, pulled one group-by request at a time
    cube = {}
    for (y, m) in months_since(C.ANOMALY_START_YEAR):
        if y not in layers:
            continue
        cube[(y, m)] = fetch_month(layers[y], y, m)
        tot = sum(cube[(y, m)].values())
        print(f"  {y}-{m:02d}: {tot:,} requests", file=sys.stderr)

    def month_total(key):
        return sum(cube[key].values())

    # Target = the latest *complete* month. The current calendar month is always
    # partial (data lags), and comparing a half-filled month to full prior months
    # would deflate everything — so drop the current month, then step back off any
    # trailing month that's still < 70% of its recent-3 median (also partial).
    now = datetime.now(timezone.utc)
    cand = [k for k in sorted(cube) if k < (now.year, now.month) and month_total(k) > 0]
    while len(cand) >= 4:
        med = sorted(month_total(k) for k in cand[-4:-1])[1]
        if month_total(cand[-1]) >= 0.7 * med:
            break
        cand.pop()
    target = cand[-1]
    ty, tm = target
    print(f"Target month: {ty}-{tm:02d} ({month_total(target):,} requests)")

    # For each (ward, code): baseline = mean of the same calendar month in the prior
    # ANOMALY_BASELINE_YEARS years; score how far the target month is above it.
    def baseline(ward, code):
        vals = []
        for k in range(1, C.ANOMALY_BASELINE_YEARS + 1):
            key = (ty - k, tm)
            if key in cube:
                vals.append(cube[key].get((ward, code), 0))
        return (sum(vals) / len(vals)) if len(vals) >= 2 else None

    rows = []  # (ward, code, recent, base, ratio, z)
    tgt = cube[target]
    codes_here = {code for (w, code) in tgt}
    for ward in WARDS:
        for code in codes_here:
            if code in exclude:
                continue
            recent = tgt.get((ward, code), 0)
            if recent < C.ANOMALY_MIN_ABS:
                continue
            base = baseline(ward, code)
            if base is None or base < 1:
                continue
            ratio = recent / base
            z = (recent - base) / max((base ** 0.5), 1.0)
            rows.append({"ward": ward, "code": code, "label": labels.get(code, code),
                         "recent": recent, "baseline": round(base, 1),
                         "ratio": round(ratio, 2), "z": round(z, 1)})

    by_ward = {}
    for ward in WARDS:
        wr = [r for r in rows if r["ward"] == ward and r["ratio"] > 1.15]
        wr.sort(key=lambda r: -r["ratio"])
        by_ward[ward] = wr[:C.ANOMALY_TOP_PER_WARD]
    board = sorted([r for r in rows if r["ratio"] > 1.25], key=lambda r: -r["z"])[:C.ANOMALY_BOARD_N]

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "month": f"{ty}-{tm:02d}",
        "baseline_desc": f"same month, prior {C.ANOMALY_BASELINE_YEARS} years",
        "wards": WARDS,
        "exclude": [c for c in C.EXCLUDE_CODES],
        "by_ward": by_ward,
        "board": board,
    }
    dest = ROOT / "agg" / "anomalies.json"
    dest.parent.mkdir(exist_ok=True)
    dest.write_text(json.dumps(out, separators=(",", ":")))
    print(f"Wrote {dest}  ({dest.stat().st_size/1024:.0f} KB) — "
          f"target {ty}-{tm:02d}, {sum(len(v) for v in by_ward.values())} ward standouts")
    if board:
        print("  top citywide standouts:")
        for r in board[:6]:
            print(f"    {r['ward']:7} {r['label'][:32]:32} {r['recent']} vs ~{r['baseline']} "
                  f"(x{r['ratio']})")


if __name__ == "__main__":
    main()
