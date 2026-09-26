"""Build sanity-gate — run AFTER the pipeline, BEFORE the Pages deploy.

Under the artifact-deploy model (DCD7) the built data no longer lives in git, so
there's no committed HEAD to diff against for most of it. Instead this fails the
job (non-zero exit) if any freshly built artifact is missing, empty, malformed,
or implausibly small — a degenerate build (truncated/empty ArcGIS pull, a
crashed builder) must NOT be deployed. On failure the workflow stops here and
the last good Pages deploy stays live.

Floors are set well below normal output so they only trip on a genuinely broken
build, not on normal month-to-month variation.

DCD13: one raw artifact IS committed to git — the per-year source-count change
token (`data/raw/source_counts.json`, DCD8/DCD10). `fetch.py` overwrites the
working-tree copy with fresh counts before this script runs, so `git show
HEAD:...` is the prior-run baseline and the working-tree copy is the new value
— a real before/after diff. This lets us catch a sudden source-side collapse
(truncated ArcGIS pull) that the existence/floor checks above can't see. We
also mirror the radar's client-side interior-zero gap logic server-side against
each signal's built weekly series, so a citywide ingestion dropout gets caught
here instead of just being displayed on the live dashboard.

Run:  python pipeline/guardrail.py
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# --- DCD13 dropout thresholds ---------------------------------------------
# Source-count collapse: current year only grows month-to-month, so any real
# shrink vs the committed baseline is a dropout; prior years are immutable, so
# they should never shrink (small increases = normal backfills, always OK).
CUR_YEAR_DROP_WARN = 0.05    # warn if current-year count is >5% below baseline
CUR_YEAR_DROP_FAIL = 0.20    # FAIL (block deploy) if >20% below — an egregious crater
PRIOR_YEAR_DROP_WARN = 0.01  # warn if a prior (frozen) year drops at all
PRIOR_YEAR_DROP_FAIL = 0.10  # FAIL if a prior year drops >10% — source truncated

# Per-signal series (agg/<key>_alerts.json):
BACKFILL_LAG_WEEKS = 2     # trailing weeks still filling in (DC lags); the UI already
                           # flags these — exclude from both checks to avoid false alarms
INTERIOR_ZERO_MIN_EXP = 3  # only flag obs==0 where seasonal expected says >=3 (matches
                           # the radar's client-side interior-zero rule)
RECENT_WEEKS = 4           # recent window to test for a below-trend collapse
RECENT_MIN_EXPECTED = 12   # skip the recent-window test below this (small-number noise)
RECENT_COLLAPSE_WARN = 0.40  # warn if recent observed < 40% of recent expected
RECENT_COLLAPSE_FAIL = 0.20  # a signal counts as "collapsed" below 20% of expected
CROSS_SIGNAL_FRAC = 0.60     # FAIL if >=60% of evaluable signals collapse at once


def _load(relpath):
    p = ROOT / relpath
    if not p.exists():
        return None, 0
    try:
        return json.loads(p.read_text()), p.stat().st_size
    except json.JSONDecodeError:
        return "MALFORMED", p.stat().st_size


def check_source_counts(errs, warns):
    """Compare fresh data/raw/source_counts.json (just written by fetch.py)
    against the committed HEAD baseline. Current year should only grow; prior
    years are frozen. A large shrink = an ingestion/source dropout, not a real
    change. Skipped (with a note) if no baseline is available."""
    fresh, _ = _load("data/raw/source_counts.json")
    if fresh in (None, "MALFORMED") or not isinstance(fresh, dict):
        errs.append(f"data/raw/source_counts.json missing or malformed ({fresh})")
        return

    try:
        proc = subprocess.run(
            ["git", "-C", str(ROOT), "show", "HEAD:data/raw/source_counts.json"],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        print("note: git unavailable — skipping source-count collapse check")
        return
    if proc.returncode != 0:
        print("note: no committed source_counts baseline — skipping collapse check")
        return
    try:
        base = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print("note: committed source_counts baseline is malformed — skipping collapse check")
        return
    if not isinstance(base, dict) or not base:
        print("note: committed source_counts baseline is empty — skipping collapse check")
        return

    cur = str(max(int(y) for y in fresh))
    for y, base_count in base.items():
        if y not in fresh or not base_count:
            continue
        drop = 1 - fresh[y] / base_count
        if drop <= 0:
            continue  # growth / backfill — never a dropout
        msg = f"source count for {y} dropped {drop:.0%}: {base_count:,} → {fresh[y]:,}"
        if y == cur:
            if drop >= CUR_YEAR_DROP_FAIL:
                errs.append(msg)
            elif drop >= CUR_YEAR_DROP_WARN:
                warns.append(msg)
        else:
            if drop >= PRIOR_YEAR_DROP_FAIL:
                errs.append(msg)
            elif drop >= PRIOR_YEAR_DROP_WARN:
                warns.append(msg)


def check_signal_series(errs, warns):
    """Server-side mirror of the radar's client-side gap logic. For each signal:
    (1) interior zeros — weeks with seasonal expected>=3 but 0 observed (WARN);
    (2) recent-window collapse — recent observed far below expected (WARN per
    signal). If most signals collapse simultaneously, that's an unambiguous
    citywide ingestion dropout -> FAIL. Trailing backfill-lag weeks are excluded
    from both (the UI already flags them)."""
    manifest, _ = _load("agg/signals.json")
    if not isinstance(manifest, dict):
        return

    collapsed = 0
    evaluable = 0
    for entry in manifest.get("signals", []):
        data, _ = _load(entry.get("file", ""))
        if data in (None, "MALFORMED") or not isinstance(data, dict):
            continue
        key = entry.get("key", entry.get("file", "?"))
        city = data.get("city", {})
        counts = city.get("counts", [])
        expected = city.get("expected", [])
        week_start = data.get("week_start", [])
        n = len(counts)
        hi = n - BACKFILL_LAG_WEEKS
        if hi <= 0:
            continue

        zeros = [
            week_start[i] for i in range(hi)
            if i < len(expected) and expected[i] is not None
            and expected[i] >= INTERIOR_ZERO_MIN_EXP and counts[i] == 0
        ]
        if zeros:
            warns.append(f"{key}: {len(zeros)} interior zero-week(s) where >=3 expected (e.g. {zeros[:5]})")

        rw = range(max(0, hi - RECENT_WEEKS), hi)
        obs = sum(counts[i] for i in rw)
        exp = sum(expected[i] for i in rw if i < len(expected) and expected[i] is not None)
        if exp < RECENT_MIN_EXPECTED:
            continue
        ratio = obs / exp
        evaluable += 1
        if ratio < RECENT_COLLAPSE_WARN:
            warns.append(f"{key}: recent {RECENT_WEEKS}wk observed {obs} vs expected {exp:.0f} ({ratio:.0%}) — possible dropout")
        if ratio < RECENT_COLLAPSE_FAIL:
            collapsed += 1

    if evaluable >= 3 and collapsed / evaluable >= CROSS_SIGNAL_FRAC:
        errs.append(f"{collapsed}/{evaluable} signals' recent weeks collapsed simultaneously — likely citywide ingestion dropout")


def main():
    errs = []
    warns = []

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

    # --- DCD13: dropout guardrails ---
    check_source_counts(errs, warns)
    check_signal_series(errs, warns)

    if warns:
        print("Guardrail warnings (non-blocking):")
        for w in warns:
            print(f"  ⚠ {w}")
    if errs:
        print("BUILD SANITY FAILED — refusing to deploy:")
        for e in errs:
            print(f"  ✗ {e}")
        sys.exit(1)
    print("Guardrail passed — all built artifacts look sane.")


if __name__ == "__main__":
    main()
