"""Build the complaint-CATEGORY breakdown for dashboard.html.

The submission-volume dashboard knows how *many* 311 requests come in and from
where (ward) — but not *what* they're about. This adds that dimension, the way
the CityCast "what did your neighbors complain about" analysis frames it: top
service types citywide and per ward, with the ever-present "trash & parking
enforcement" categories separable so local outliers surface.

Cheap to build: one server-side group-by request per year layer (WARD x
SERVICECODE, counted) — no row downloads. Writes agg/categories.json, fetched at
runtime by dashboard.html (kept out of the embedded DATA blob to avoid bloating
the page).

Run:  ./.venv/bin/python pipeline/categories.py   (idempotent)
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
OTHER = "__other__"
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


def year_layers():
    meta = _get(C.ARCGIS_SERVICE, {"f": "json"})
    out = {}
    for lyr in meta["layers"]:
        name = lyr["name"]
        if name.startswith("All Service Requests - "):
            tail = name.rsplit("-", 1)[-1].strip()
            if tail.isdigit():
                out[int(tail)] = lyr["id"]
    return out


def fetch_year_matrix(layer_id):
    """One group-by request -> {(ward, code): count}, plus {code: description}."""
    res = _get(
        f"{C.ARCGIS_SERVICE}/{layer_id}/query",
        {"where": "1=1",
         "groupByFieldsForStatistics": "WARD,SERVICECODE,SERVICECODEDESCRIPTION",
         # NB: the stat value only serializes under a plain alias like CNT on this
         # server (a name of "n" comes back null) — don't rename without testing.
         "outStatistics": json.dumps([{"statisticType": "count",
                                       "onStatisticField": "SERVICECODE",
                                       "outStatisticFieldName": "CNT"}]),
         "f": "json"},
    )
    cells, labels = defaultdict(int), {}
    for f in res.get("features", []):
        a = f["attributes"]
        code = a.get("SERVICECODE")
        n = a.get("CNT") or 0
        if not code or not n:
            continue
        ward = (a.get("WARD") or "").strip() or "Unknown"
        cells[(ward, code)] += n
        desc = a.get("SERVICECODEDESCRIPTION")
        if desc and code not in labels:
            labels[code] = desc
    return cells, labels


def main():
    layers = year_layers()
    years = sorted(y for y in layers if y >= C.CATEGORY_START_YEAR)
    print(f"Building category cube for {years[0]}-{years[-1]} "
          f"({len(years)} year layers)…")

    # year -> {(ward, code): count}
    per_year = {}
    labels = {}
    alltime = defaultdict(int)  # code -> total (for ranking)
    for y in years:
        cells, lbls = fetch_year_matrix(layers[y])
        per_year[y] = cells
        for code, desc in lbls.items():
            labels.setdefault(code, desc)
        for (ward, code), n in cells.items():
            alltime[code] += n
        print(f"  {y}: {sum(cells.values()):,} requests, "
              f"{len({c for _, c in cells})} codes", file=sys.stderr)

    # Keep the top-N codes citywide; always keep the excluded (trash/parking)
    # codes too so the toggle can add them back. Everything else -> "Other".
    ranked = [c for c, _ in sorted(alltime.items(), key=lambda kv: -kv[1])]
    keep = set(ranked[:C.CATEGORY_TOP_N]) | set(C.EXCLUDE_CODES)

    def fold(code):
        return code if code in keep else OTHER

    citywide, by_ward = {}, {}
    for y in years:
        ys = str(y)
        cw = defaultdict(int)
        bw = {w: defaultdict(int) for w in WARDS}
        for (ward, code), n in per_year[y].items():
            fc = fold(code)
            cw[fc] += n
            if ward in bw:
                bw[ward][fc] += n
        citywide[ys] = dict(cw)
        by_ward[ys] = {w: dict(v) for w, v in bw.items() if v}

    out_labels = {c: labels.get(c, c) for c in keep}
    out_labels[OTHER] = "Other categories"

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_through": str(years[-1]),
        "years": years,
        "wards": WARDS,
        "labels": out_labels,
        "exclude": [c for c in C.EXCLUDE_CODES if c in keep],
        "citywide": citywide,
        "by_ward": by_ward,
    }
    dest = ROOT / "agg" / "categories.json"
    dest.parent.mkdir(exist_ok=True)
    dest.write_text(json.dumps(out, separators=(",", ":")))
    print(f"Wrote {dest}  ({dest.stat().st_size/1024:.0f} KB) — "
          f"{len(keep)} codes kept, {len(years)} years")
    # quick sanity: latest year's top-3 non-excluded categories citywide
    ex = set(out["exclude"])
    latest = sorted(((n, c) for c, n in citywide[str(years[-1])].items()
                     if c not in ex and c != OTHER), reverse=True)[:3]
    print("  latest-year top non-trash/parking:",
          ", ".join(f"{out_labels[c]}={n:,}" for n, c in latest))


if __name__ == "__main__":
    main()
