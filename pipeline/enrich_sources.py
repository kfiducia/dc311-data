"""Pull each new day's SR source/origin from DC's live 311 API ->
data/sources/<year>.csv, the input to refresh_methods.py --distill.

ArcGIS (fetch.py) has no submission method. The live API behind DC's 311 app
does: /311/v4/requests.json?start_date=…&end_date=… returns full SR records,
newest first, at most 50 per call. Each UTC day is cursor-paged backward on
the oldest requested_datetime until a page comes back short (= the whole day),
so the CSV is the complete population of what the API serves, with no
time-of-day bias. ~1 call per 50 SRs, ~45 s/day; a week is ~150 calls.

Coverage is ~half of ArcGIS, split by REQUEST TYPE: ~60 types (parking,
DMV, info calls, yard waste, snow …) are never served, even by id; rodents and
illegal dumping only from 2026-05-01. See README "Why the API has gaps".

Append-only and resumable: the CSV is never rewritten. A day is appended to
<year>.done only after its rows are fsync'd, so a run stopped at any point
resumes at the first unfinished day without re-requesting finished ones, and a
re-pulled day appends only ids not already on disk. Today (UTC) is never pulled
(a partial day would be marked done). An API failure raises instead of
recording an empty day. Sequential on purpose: ~8 concurrent calls get a 403.
The API is behind Cloudflare bot management: it needs a browser User-Agent and
is known to work from a home IP (methods.yml runs on the homelab k3s runner).

Years pulled by default: the current year, plus last year while its .done is
missing Dec 31 (January catch-up). 2016-2025 are a frozen every-other-day
capture (no .done) and are never touched.

Needs DC311_API_KEY: the public api_key DC's own 311 app embeds (a repo
secret here, never committed). MAX_MINUTES stops cleanly between days so a CI
job can't run away; the next run resumes.

Run:  DC311_API_KEY=… python pipeline/enrich_sources.py
      YEARS=2026 REDO=2026-06-17 … python pipeline/enrich_sources.py   # force re-pull a day
"""
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError

API = "https://dc311-api.herokuapp.com/311/v4/requests.json"
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
SRC = Path(__file__).resolve().parent.parent / "data" / "sources"
COLS = ["service_request_id", "requested_datetime", "source", "origin",
        "service_code", "service_name"]
CAP = 50  # API max rows per call

_calls = 0


def _get(key, start, end, retries=6):
    global _calls
    q = urllib.parse.urlencode({
        "api_key": key, "language": "en",
        "start_date": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_date": end.strftime("%Y-%m-%dT%H:%M:%SZ")})
    req = urllib.request.Request(f"{API}?{q}",
                                 headers={"User-Agent": UA, "Accept": "application/json"})
    for attempt in range(1, retries + 1):
        _calls += 1
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                rows = json.loads(r.read().decode("utf-8") or "[]")
            time.sleep(0.2)
            return rows
        except HTTPError as e:
            if e.code == 400:
                return []  # degenerate (zero-width) window
            err = f"HTTP {e.code}"
            time.sleep(4 * attempt)
        except (URLError, TimeoutError, json.JSONDecodeError) as e:
            err = repr(e)
            time.sleep(2 * attempt)
        print(f"    retry {attempt}/{retries}: {err}", flush=True)
    # Raise rather than return [] -- an empty result would mark the day done
    # with no rows, silently losing it. The run stops; rerun to resume.
    raise RuntimeError(f"DC 311 API failed {retries}x for {start}..{end}: {err}")


def _ts(s):
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def collect_day(key, day):
    """Every SR the API serves with requested_datetime in [day, day+1), deduped."""
    cur_end = day + timedelta(days=1)
    recs = {}
    while cur_end > day:
        rows = _get(key, day, cur_end)
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
    return list(recs.values())


def existing(path):
    """(ids, days) already in the CSV."""
    ids, days = set(), set()
    if path.exists():
        with path.open(newline="") as fh:
            for row in csv.DictReader(fh):
                ids.add(row["service_request_id"])
                if row["requested_datetime"][:10]:
                    days.add(row["requested_datetime"][:10])
    return ids, days


def load_done(done_path, csv_days):
    """Days fully captured. With no ledger yet, seed it from the CSV's days minus
    the last one: that's the day an earlier run may have cut short (how
    2026-06-17 once ended up with 81 of ~870 SRs). REDO=YYYY-MM-DD,… forces
    specific days to be re-pulled."""
    if not done_path.exists():
        seed = sorted(csv_days)[:-1]
        done_path.write_text("".join(d + "\n" for d in seed))
        print(f"  seeded {done_path.name} with {len(seed)} days", flush=True)
    done = {l.strip() for l in done_path.read_text().splitlines() if l.strip()}
    return done - {d.strip() for d in os.environ.get("REDO", "").split(",") if d.strip()}


def run_year(key, year, deadline):
    out = SRC / f"{year}.csv"
    done_path = SRC / f"{year}.done"
    have_ids, csv_days = existing(out)
    done = load_done(done_path, csv_days)
    pre = f"{year % 100:02d}-"
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    first = datetime(year, 1, 1, tzinfo=timezone.utc)
    last = min(datetime(year + 1, 1, 1, tzinfo=timezone.utc), today)
    todo = [first + timedelta(days=i) for i in range((last - first).days)
            if (first + timedelta(days=i)).date().isoformat() not in done]
    print(f"{year}: {len(have_ids):,} rows on disk, {len(done)} days done, {len(todo)} to pull",
          flush=True)
    new = not out.exists()
    kept, recent, t0 = 0, [], time.time()
    with out.open("a", newline="") as fh, done_path.open("a") as dfh:
        w = csv.writer(fh)
        if new:
            w.writerow(COLS)
        for n, day in enumerate(todo, 1):
            if time.time() > deadline:
                print(f"  MAX_MINUTES reached — stopping; {len(todo) - n + 1} days left for "
                      "the next run", flush=True)
                return False
            calls0 = _calls
            day_n = added = 0
            for x in collect_day(key, day):
                sid = str(x.get("service_request_id", ""))
                if sid.startswith(pre) and x.get("source") and \
                        (x.get("requested_datetime") or "")[:10] == day.date().isoformat():
                    day_n += 1
                    if sid not in have_ids:
                        w.writerow([sid, x.get("requested_datetime"), x.get("source"),
                                    x.get("origin"), x.get("service_code"), x.get("service_name")])
                        have_ids.add(sid)
                        added += 1
            fh.flush(); os.fsync(fh.fileno())
            ds = day.date().isoformat()
            dfh.write(ds + "\n"); dfh.flush(); os.fsync(dfh.fileno())
            kept += added
            # flag a day far below the recent norm (API trouble, or a genuinely quiet day)
            med = sorted(recent)[len(recent) // 2] if len(recent) >= 7 else None
            warn = f"  <-- LOW vs recent median {med}, check" if med and day_n < 0.35 * med else ""
            recent = (recent + [day_n])[-14:]
            el = time.time() - t0
            print(f"  {ds} {day:%a}: {day_n:5,} SRs (+{added:,} new) {_calls - calls0:3} calls | "
                  f"{n}/{len(todo)} days, +{kept:,} rows, {el / 60:.0f} min, "
                  f"~{el / n * (len(todo) - n) / 60:.0f} min left{warn}", flush=True)
    print(f"{year}: +{kept:,} rows ({len(have_ids):,} total)", flush=True)
    return True


def default_years():
    y = datetime.now(timezone.utc).year
    prev = SRC / f"{y - 1}.done"
    finishing = prev.exists() and f"{y - 1}-12-31" not in prev.read_text()
    return ([y - 1] if finishing else []) + [y]


def main():
    key = os.environ.get("DC311_API_KEY")
    if not key:
        sys.exit("DC311_API_KEY is not set (the api_key DC's 311 app embeds).")
    years = [int(y) for y in os.environ["YEARS"].split(",")] if os.environ.get("YEARS") \
        else default_years()
    deadline = time.time() + 60 * float(os.environ.get("MAX_MINUTES", "inf"))
    SRC.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    for y in years:
        if not run_year(key, y, deadline):
            break
    print(f"Done. {_calls:,} API calls, {(time.time() - t0) / 60:.0f} min")


if __name__ == "__main__":
    main()
