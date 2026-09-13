"""Freshness / sanity guardrail — run AFTER the pipeline, BEFORE committing.

The whole point of the automated monthly refresh is that it must never quietly
commit gap-ridden or empty data over good data. DC's ArcGIS endpoint can return
a truncated or empty response that still "succeeds" (HTTP 200), which would
otherwise regenerate a smaller-but-valid-looking agg.json and silently wipe out
real history on the live dashboard.

So this script compares the freshly regenerated artifacts in the working tree
against the versions committed at HEAD (the last good refresh) and FAILS the job
(non-zero exit) if either:

  * the total row/report count dropped sharply (> DROP_FRAC below HEAD), or
  * the data window went BACKWARD (newest month / week older than HEAD).

A window that merely didn't advance is NOT a failure — DC's data lags and
backfills, so a monthly run can legitimately land before new data is posted; in
that case the "commit only when the diff is non-empty" step in the workflow
means nothing gets committed anyway. We warn about it but don't fail.

Checked artifacts:
  * agg.json                  (submission-volume dashboard)
  * agg/rodent_alerts.json    (complaint-radar / early-warning dashboard)
  * agg/categories.json       (dashboard complaint-category breakdown)

If an artifact has no committed HEAD version yet (first run), its checks are
skipped — there's nothing to regress against.

Run:  python pipeline/guardrail.py
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# A refresh only ever ADDS rows (history is immutable, the current period grows),
# so any real drop signals a truncated/empty pull. Allow a tiny tolerance.
DROP_FRAC = 0.95  # fail if new_total < 95% of the committed total


def head_json(relpath):
    """Parse the committed HEAD version of a file, or None if it doesn't exist."""
    try:
        blob = subprocess.run(
            ["git", "show", f"HEAD:{relpath}"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout
    except subprocess.CalledProcessError:
        return None
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        return None


def working_json(relpath):
    p = ROOT / relpath
    return json.loads(p.read_text()) if p.exists() else None


def check(name, relpath, total_of, through_of):
    """Return (errors, warnings) for one artifact."""
    errs, warns = [], []
    new = working_json(relpath)
    old = head_json(relpath)
    if new is None:
        errs.append(f"[{name}] working-tree {relpath} is missing or invalid.")
        return errs, warns
    if old is None:
        print(f"[{name}] no committed HEAD version — first run, skipping regression checks.")
        return errs, warns

    old_total, new_total = total_of(old), total_of(new)
    old_thru, new_thru = through_of(old), through_of(new)
    print(f"[{name}] total: {old_total:,} -> {new_total:,} | "
          f"through: {old_thru} -> {new_thru}")

    if old_total and new_total < DROP_FRAC * old_total:
        errs.append(f"[{name}] total dropped sharply: {new_total:,} < "
                    f"{DROP_FRAC:.0%} of {old_total:,} — likely a truncated/empty pull.")
    if old_thru and new_thru and str(new_thru) < str(old_thru):
        errs.append(f"[{name}] data window went BACKWARD: {new_thru} < {old_thru}.")
    elif old_thru and new_thru and str(new_thru) == str(old_thru):
        warns.append(f"[{name}] data window did not advance (still {new_thru}); "
                     f"DC data may just be lagging — not fatal.")
    return errs, warns


def agg_total(d):
    return d.get("bulk_total") or sum(c for _, c in d.get("by_year", []))


def agg_through(d):
    if d.get("data_through"):
        return d["data_through"]
    bm = d.get("by_month") or []
    return bm[-1][0] if bm else None


def rodent_total(d):
    city = d.get("city") or {}
    return sum(city.get("counts", [])) or sum(u.get("total", 0) for u in d.get("units", []))


def rodent_through(d):
    if d.get("data_through"):
        return d["data_through"]
    ws = d.get("week_start") or []
    return ws[-1] if ws else None


def cat_total(d):
    # every request across every (year, ward, category) in the cube
    return sum(n for yr in (d.get("citywide") or {}).values() for n in yr.values())


def cat_through(d):
    return d.get("data_through")


def main():
    errors, warnings = [], []
    for args in (
        ("agg.json", "agg.json", agg_total, agg_through),
        ("rodent_alerts", "agg/rodent_alerts.json", rodent_total, rodent_through),
        ("categories", "agg/categories.json", cat_total, cat_through),
    ):
        e, w = check(*args)
        errors += e
        warnings += w

    for w in warnings:
        print(f"WARN: {w}")
    if errors:
        print("\nGUARDRAIL FAILED — refusing to commit:")
        for e in errors:
            print(f"  ✗ {e}")
        sys.exit(1)
    print("\nGuardrail passed.")


if __name__ == "__main__":
    main()
