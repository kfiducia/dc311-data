"""Build sanity-gate — run AFTER the pipeline, BEFORE the Pages deploy.

Under the artifact-deploy model (DCD7) the built data no longer lives in git, so
there's no committed HEAD to diff against. Instead this fails the job (non-zero
exit) if any freshly built artifact is missing, empty, malformed, or implausibly
small — a degenerate build (truncated/empty ArcGIS pull, a crashed builder) must
NOT be deployed. On failure the workflow stops here and the last good Pages
deploy stays live.

Floors are set well below normal output so they only trip on a genuinely broken
build, not on normal month-to-month variation.

Run:  python pipeline/guardrail.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(relpath):
    p = ROOT / relpath
    if not p.exists():
        return None, 0
    try:
        return json.loads(p.read_text()), p.stat().st_size
    except json.JSONDecodeError:
        return "MALFORMED", p.stat().st_size


def main():
    errs = []

    def require(cond, msg):
        if not cond:
            errs.append(msg)

    # --- submission-volume dashboard ---
    agg, _ = _load("agg.json")
    if agg in (None, "MALFORMED"):
        require(False, f"agg.json missing or malformed ({agg})")
    else:
        require((agg.get("bulk_total") or 0) >= 4_000_000,
                f"agg.json bulk_total too low: {agg.get('bulk_total')}")
        require(len(agg.get("by_month", [])) > 100,
                f"agg.json by_month too short: {len(agg.get('by_month', []))}")
        require(bool(agg.get("data_through")), "agg.json missing data_through")

    # --- Complaint Radar: the rat signal is always present ---
    rr, _ = _load("agg/rodent_alerts.json")
    if rr in (None, "MALFORMED"):
        require(False, f"agg/rodent_alerts.json missing or malformed ({rr})")
    else:
        require(len(rr.get("units", [])) >= 50,
                f"rodent_alerts units too few: {len(rr.get('units', []))}")
        require(bool(rr.get("week_start")), "rodent_alerts missing week_start")

    # --- radar manifest ---
    sig, _ = _load("agg/signals.json")
    require(isinstance(sig, dict) and len(sig.get("signals", [])) >= 1,
            "agg/signals.json missing or has no signals")

    # --- SMD chart + choropleth ---
    smd, _ = _load("agg/smd.json")
    if smd in (None, "MALFORMED"):
        require(False, f"agg/smd.json missing or malformed ({smd})")
    else:
        require(len(smd.get("smds", [])) >= 300,
                f"smd.json has too few SMDs: {len(smd.get('smds', []))}")
        require(bool(smd.get("years")), "smd.json missing years")
        latest = str(smd.get("years", [0])[-1]) if smd.get("years") else None
        mapped = sum((smd.get("counts", {}).get(latest, {}) or {}).get("__all__", [])) if latest else 0
        require(mapped > 0, f"smd.json latest-year mapped count is 0 ({latest})")
    geo, geo_sz = _load("agg/smd_boundaries.min.geojson")
    require(isinstance(geo, dict) and len(geo.get("features", [])) >= 300,
            "smd_boundaries.min.geojson missing or too few polygons")
    require(geo_sz < 1_500_000,
            f"smd_boundaries.min.geojson too large for Pages: {geo_sz} B (simplify harder)")

    # --- category + anomaly boards (existence + non-empty) ---
    cat, _ = _load("agg/categories.json")
    require(isinstance(cat, dict) and bool(cat.get("citywide")),
            "agg/categories.json missing or empty")
    anom, _ = _load("agg/anomalies.json")
    require(isinstance(anom, dict) and "board" in anom,
            "agg/anomalies.json missing or empty")

    if errs:
        print("BUILD SANITY FAILED — refusing to deploy:")
        for e in errs:
            print(f"  ✗ {e}")
        sys.exit(1)
    print("Guardrail passed — all built artifacts look sane.")


if __name__ == "__main__":
    main()
