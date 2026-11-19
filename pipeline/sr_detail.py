"""Pull the FULL detail record for every DC 311 request in a date range from DC's
live 311 API -> data/detail/<year>/<YYYY-MM-DD>.jsonl.gz (one SR JSON per line).

The ArcGIS bulk dataset (fetch.py) has no description, source/origin, ANC/SMD,
SLA, closure reason, media_url, etc. The live API behind DC's 311 app does:
  /311/v4/requests.json?start_date=…&end_date=…   (full records, newest-first)
It returns at most 50 rows per query, so each day is cursor-paged backward on
the oldest requested_datetime until a page comes back short (= everything in the
window). ~16 calls / ~45 s per day, so a full year is ~6k calls / ~4-5 hours.
Sequential on purpose: ~8 concurrent requests trips a 403 at DC's front door.

Coverage is ~half of ArcGIS, and that is DC's API, not this pager: whole service
types (Parking Enforcement, DC Government Information, Scheduled Yard Waste,
DMV, ROSA …) are never served, not even by service_request_id. Measured
2026-06-10: 807 via the API vs 1,568 in ArcGIS; sampled misses all came back
empty by id.

Records are a snapshot: an SR still open when pulled stays "open" here. Re-pull
a range with --refresh to pick up closures. Today (UTC) is never pulled — it's
incomplete.

Relocated from home-rule-org/dc311/scripts/enrich_complete.py, which kept only
6 columns (id, datetime, source, origin, service code/name); this keeps the
whole record.

Needs DC311_API_KEY — the public api_key DC's own 311 app embeds (not committed
here because this repo is public).

Run:  DC311_API_KEY=… ./.venv/bin/python pipeline/sr_detail.py                # trailing 365 days
      DC311_API_KEY=… ./.venv/bin/python pipeline/sr_detail.py 2025-01-01 2025-12-31
      … --refresh   re-pull days that already have a file
Resumable: a day is skipped once its file exists (written atomically).
"""
import gzip
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError

API = "https://dc311-api.herokuapp.com/311/v4/requests.json"
# DC's API sits behind Cloudflare bot management, which blocks the default
# Python User-Agent.
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
CAP = 50  # API max rows per query
OUT = Path(__file__).resolve().parent.parent / "data" / "detail"

_calls = 0


def _get(key, start, end, retries=6):
    """One window query. Raises after `retries` so a dead API can't silently
    write empty days (the old enricher returned [] and moved on)."""
    global _calls
    q = urllib.parse.urlencode({
        "api_key": key, "language": "en",
        "start_date": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_date": end.strftime("%Y-%m-%dT%H:%M:%SZ")})
    req = urllib.request.Request(f"{API}?{q}",
                                 headers={"User-Agent": UA, "Accept": "application/json"})
    for attempt in range(1, retries + 1):
        try:
            _calls += 1
            with urllib.request.urlopen(req, timeout=30) as r:
                rows = json.loads(r.read().decode("utf-8") or "[]")
            time.sleep(0.2)
            return rows
        except HTTPError as e:
            if e.code == 400:
                return []  # degenerate (zero-width) window
            err = e
        except (URLError, TimeoutError, json.JSONDecodeError) as e:
            err = e
        print(f"  retry {attempt} ({err})", file=sys.stderr)
        time.sleep(4 * attempt)
    raise RuntimeError(f"DC 311 API failed {retries}x for {start}..{end}: {err}")


def _ts(s):
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def collect_day(key, day):
    """Every SR the API serves with requested_datetime in [day, day+1), by id."""
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    cur_end = start + timedelta(days=1)
    recs = {}
    while cur_end > start:
        rows = _get(key, start, cur_end)
        oldest = None
        for r in rows:
            sid = r.get("service_request_id")
            if sid:
                recs.setdefault(sid, r)
            ts = r.get("requested_datetime")
            if ts and (oldest is None or _ts(ts) < oldest):
                oldest = _ts(ts)
        if len(rows) < CAP:
            break
        # page backward from the oldest row seen; nudge on a timestamp tie
        new_end = oldest or cur_end - timedelta(seconds=1)
        cur_end = new_end if new_end < cur_end else cur_end - timedelta(seconds=1)
    # keep only this day's rows, so a row on the end bound isn't stored twice
    return [r for r in recs.values()
            if (r.get("requested_datetime") or "")[:10] == day.isoformat()]


def write_day(day, recs):
    path = OUT / str(day.year) / f"{day.isoformat()}.jsonl.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(".gz.part")
    with gzip.open(part, "wt") as fh:
        for r in sorted(recs, key=lambda r: r.get("requested_datetime") or ""):
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")
    part.rename(path)
    return path


def main():
    key = os.environ.get("DC311_API_KEY")
    if not key:
        sys.exit("DC311_API_KEY is not set (the api_key DC's 311 app embeds).")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    refresh = "--refresh" in sys.argv
    today = datetime.now(timezone.utc).date()
    last = min(date.fromisoformat(args[1]) if len(args) > 1 else today, today - timedelta(days=1))
    first = date.fromisoformat(args[0]) if args else last - timedelta(days=364)
    print(f"SR detail {first} .. {last} -> {OUT}")

    total, t0, day = 0, time.time(), first
    while day <= last:
        path = OUT / str(day.year) / f"{day.isoformat()}.jsonl.gz"
        if refresh or not path.exists():
            recs = collect_day(key, day)
            write_day(day, recs)
            total += len(recs)
            print(f"  {day}: {len(recs):,} SRs  ({total:,} total, {_calls:,} calls, "
                  f"{(time.time() - t0) / 60:.0f} min)", flush=True)
        day += timedelta(days=1)
    print(f"Done. {total:,} SRs, {_calls:,} calls, {(time.time() - t0) / 60:.0f} min")


if __name__ == "__main__":
    main()
