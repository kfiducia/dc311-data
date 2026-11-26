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
VOLUME_TOL = 0.01            # DCD16: published past-year total vs live layer count

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


def check_volume_completeness(agg, errs, warns):
    """DCD16: every complete past year's published by_year total must match DC's
    live per-year layer count (data/raw/source_counts.json, just written by
    fetch.py) to within VOLUME_TOL. Catches a past year frozen at a mid-year
    snapshot — 2024/2025 shipped ~6%/~19% short for a year because nothing
    re-finished them after the year rolled over."""
    counts, _ = _load("data/raw/source_counts.json")
    if not isinstance(counts, dict) or not counts:
        return  # check_source_counts already reports a missing/malformed file
    cur = max(int(y) for y in counts)
    published = dict(agg.get("by_year", []))
    for y, hours in (agg.get("hour_local_by_year") or {}).items():
        pub = published.get(int(y)) or 0
        # warn only: the hour chart is cosmetic and refresh_submission keeps the
        # previous hours when a re-pull comes back short
        if pub and abs(sum(hours) - pub) > VOLUME_TOL * pub:
            warns.append(f"agg.json hour_local_by_year {y} sums to {sum(hours):,} "
                        f"vs by_year {pub:,} — truncated hour-of-day pull?")
    for y, live in sorted(counts.items()):
        y = int(y)
        if y >= cur or not live:
            continue
        pub = published.get(y)
        if pub is None:
            errs.append(f"agg.json by_year missing {y} (live count {live:,})")
        elif abs(pub - live) > VOLUME_TOL * live:
            errs.append(f"agg.json by_year {y} = {pub:,} vs live {live:,} "
                        f"({pub / live - 1:+.1%}) — past year not refreshed?")


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


def check_rats_export(require, warns):
    """agg/rats/ is consumed by another site (abatement.home-rule.org), which loads
    whatever it finds — so a truncated or inconsistent export must not deploy. The
    consumer runs its own row-count guard too; this is the first line of defence."""
    import hashlib
    from datetime import date
    meta, _ = _load("agg/rats/meta.json")
    if meta in (None, "MALFORMED"):
        require(False, f"agg/rats/meta.json missing or malformed ({meta})")
        return
    require(meta.get("schema") == 1, f"rats meta schema {meta.get('schema')!r} != 1")
    totals = meta.get("totals") or {}
    require((totals.get("reports") or 0) >= 100_000,
            f"rats export too few reports: {totals.get('reports')}")
    require((totals.get("blocks") or 0) >= 1_000,
            f"rats export too few shaded blocks: {totals.get('blocks')}")
    recent, inside = totals.get("recent_reports") or 0, totals.get("recent_in_block") or 0
    require(recent and inside / recent >= 0.9,
            f"only {inside}/{recent} recent rat reports landed in a block (squares fetch broken?)")
    through = meta.get("data_through") or ""
    try:
        age = (date.today() - date.fromisoformat(through)).days
        if age > 45:
            warns.append(f"rats export data_through {through} is {age} days old")
    except ValueError:
        require(False, f"rats export bad data_through {through!r}")
    files = meta.get("files") or {}
    entries = list(files.get("reports") or []) + [files.get("smd") or {}, files.get("blocks") or {}]
    for f in entries:
        p = ROOT / "agg" / "rats" / (f.get("path") or "?")
        ok = p.exists() and hashlib.sha256(p.read_bytes()).hexdigest() == f.get("sha256")
        require(ok, f"rats export file {f.get('path')} missing or sha256 mismatch")


def check_area_trends(require, warns):
    """agg/area_trends/ (area_trends.py): the index must list every area, every
    area file must exist and parse, the SMD, ANC and ward files must each add up
    to the city total, every ward must hold an SMD, and the files must stay small
    enough to load one area at a time. A weak SMD ward vote is a warning.

    Run on its own (`--only area-trends`) by refresh.yml, NOT from main(): a broken
    area-trends build drops agg/area_trends/ from that deploy instead of blocking
    the rest of the site (and the rat feed)."""
    base = ROOT / "agg" / "area_trends"
    idx, _ = _load("agg/area_trends/index.json")
    if not isinstance(idx, dict):
        require(False, f"agg/area_trends/index.json missing or malformed ({idx})")
        return
    areas = idx.get("areas") or []
    months = idx.get("months") or []
    smds = [a for a in areas if a.get("level") == "smd"]
    require(len(smds) >= 300, f"area_trends: too few SMDs ({len(smds)})")
    require(sum(a.get("level") == "ward" for a in areas) == 8, "area_trends: expected 8 wards")
    require(len(months) > 100, f"area_trends: too few months ({len(months)})")
    require(len(idx.get("categories") or []) >= 10, "area_trends: too few categories")
    require(bool(idx.get("presets")), "area_trends: no presets")
    require(all("/" not in a["id"] for a in areas), "area_trends: an area id is not filename-safe")
    city, _ = _load("agg/area_trends/city.json")
    if not isinstance(city, dict):
        require(False, "agg/area_trends/city.json missing or malformed")
        return
    tot = city.get("all") or []
    require(len(tot) == len(months), "area_trends: city series length != months")
    require(sum(tot) >= 3_000_000, f"area_trends: city total too low ({sum(tot):,})")
    last_full = tot[-2] if idx.get("partial_last") and len(tot) > 1 else (tot[-1] if tot else 0)
    require(last_full > 10_000, f"area_trends: latest complete month only {last_full:,} requests")
    sums = {lvl: [0] * len(months) for lvl in ("ward", "anc", "smd")}
    missing, bad, biggest = [], [], 0
    for a in areas:
        p = base / f"{a['id']}.json"
        if not p.exists():
            missing.append(a["id"])
            continue
        biggest = max(biggest, p.stat().st_size)
        lvl = a.get("level")
        if lvl in sums:
            try:
                series = json.loads(p.read_text()).get("all") or []
            except json.JSONDecodeError:
                bad.append(a["id"])
                continue
            for i, v in enumerate(series[:len(months)]):
                sums[lvl][i] += v
    require(not missing, f"area_trends: {len(missing)} area file(s) missing, e.g. {missing[:3]}")
    require(not bad, f"area_trends: {len(bad)} malformed area file(s), e.g. {bad[:3]}")
    for lvl, s in sums.items():
        require(s == tot, f"area_trends: {lvl.upper()} files don't add up to the city total")
    smd_wards = {a.get("ward") for a in smds}
    empty = [a["id"] for a in areas if a.get("level") == "ward" and a["id"] not in smd_wards]
    require(not empty, f"area_trends: ward(s) with no SMD: {empty}")
    require(biggest < 200_000, f"area_trends: largest area file {biggest:,} B (should be ~30 KB)")
    for s, share in (idx.get("ward_vote") or {}).get("weak") or []:
        warns.append(f"area_trends: SMD {s} ward vote only {share:.0%} — check its ward")


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
        check_volume_completeness(agg, errs, warns)

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

    # (area-trends is checked separately: `guardrail.py --only area-trends`)

    # --- rat export for the abatement coordinator (a published contract — see rats.py)
    check_rats_export(require, warns)

    # --- DCD13: dropout guardrails ---
    check_source_counts(errs, warns)
    check_signal_series(errs, warns)
    report(errs, warns, "BUILD SANITY FAILED — refusing to deploy:",
           "Guardrail passed — all built artifacts look sane.")


def report(errs, warns, fail_title, ok_msg):
    if warns:
        print("Guardrail warnings (non-blocking):")
        for w in warns:
            print(f"  ⚠ {w}")
    if errs:
        print(fail_title)
        for e in errs:
            print(f"  ✗ {e}")
        sys.exit(1)
    print(ok_msg)


def main_area_trends():
    """`--only area-trends`: exit 1 if agg/area_trends/ is broken. refresh.yml then
    drops that directory from the deploy rather than failing the whole run."""
    errs, warns = [], []
    check_area_trends(lambda ok, msg: ok or errs.append(msg), warns)
    report(errs, warns, "AREA TRENDS SANITY FAILED:", "Area-trends guardrail passed.")


if __name__ == "__main__":
    if sys.argv[1:] == ["--only", "area-trends"]:
        main_area_trends()
    elif sys.argv[1:]:
        raise SystemExit("usage: guardrail.py [--only area-trends]")
    else:
        main()
