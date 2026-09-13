"""Config for the DC 311 early-warning pipeline.

The detection engine is parameterized on **signals** so the same seasonal-
aberration detector can be pointed at any 311 service type. Rats are the one
special, hand-configured signal (they get a dead-animal corroborating overlay);
every other radar signal is picked **data-driven** — the top-N service categories
by volume, minus the ever-present trash/parking/info ones — so the radar is a
generic "point the method at whatever's biggest," not a curated topic list.
That's `resolve_signals()`; fetch.py and build.py call it.

Each signal:
  key    short slug -> data/raw/<key>_reports.csv and agg/<key>_alerts.json
  label  human name shown in the dashboard's signal picker
  codes  list of ArcGIS SERVICECODE values that make up the signal, OR
  where  an explicit ArcGIS WHERE clause (used instead of `codes` for a family)
  aux    corroborating signals fetched + overlaid on the timeline (not detected
         on) — same {key,label,codes} shape.
"""
import json as _json
import re as _re
import urllib.parse as _urlparse
import urllib.request as _urlreq

# --- Rats: the one special signal (dead-animal overlay), always the default ---
RODENT = {
    "key": "rodent",
    "label": "Rats & rodents",
    "codes": ["S0311"],  # "Rodent Inspection and Treatment" (a.k.a. Health R&V Control)
    # Dead-animal pickups track rat activity (poisoning die-off, carcasses).
    "aux": [{"key": "dead_animal", "label": "Dead-animal pickups", "codes": ["11"]}],
}

# How many *additional* data-driven signals (beyond rats) the radar offers.
RADAR_TOP_N = 8

# Codes never offered as their own radar signal: the ever-present categories
# (see EXCLUDE_CODES below) plus the ones already spoken for (rats, dead-animal).
_RADAR_SKIP = {"S0311", "11"}


def signal_where(sig):
    """ArcGIS WHERE clause for a signal (explicit `where` wins over `codes`)."""
    if sig.get("where"):
        return sig["where"]
    codes = sig.get("codes") or []
    quoted = ",".join("'" + c.replace("'", "''") + "'" for c in codes)
    return f"SERVICECODE IN ({quoted})"


def _slug(code):
    return _re.sub(r"[^a-z0-9]", "", code.lower()) or "sig"


def _latest_layer():
    meta = _json.load(_urlreq.urlopen(f"{ARCGIS_SERVICE}?f=json", timeout=120))
    years = {}
    for lyr in meta["layers"]:
        n = lyr["name"]
        if n.startswith("All Service Requests - "):
            t = n.rsplit("-", 1)[-1].strip()
            if t.isdigit():
                years[int(t)] = lyr["id"]
    return years[max(years)]


def resolve_signals():
    """[RODENT] + the top-N non-ubiquitous categories by volume, as single-code
    signals — queried live so the radar tracks whatever's actually biggest, not a
    hand-picked list. Falls back to just rats if the catalog query fails."""
    try:
        lid = _latest_layer()
        params = {
            "where": "1=1",
            "groupByFieldsForStatistics": "SERVICECODE,SERVICECODEDESCRIPTION",
            "outStatistics": _json.dumps([{"statisticType": "count",
                                           "onStatisticField": "SERVICECODE",
                                           "outStatisticFieldName": "CNT"}]),
            "orderByFields": "CNT DESC", "f": "json",
        }
        res = _json.load(_urlreq.urlopen(
            f"{ARCGIS_SERVICE}/{lid}/query?{_urlparse.urlencode(params)}", timeout=120))
    except Exception:  # noqa: BLE001 — network hiccup: degrade to rats-only
        return [RODENT]
    skip = _RADAR_SKIP | set(EXCLUDE_CODES)
    picked, seen = [], set()
    for f in res.get("features", []):
        a = f["attributes"]
        code, desc, n = a.get("SERVICECODE"), a.get("SERVICECODEDESCRIPTION"), a.get("CNT")
        if not code or not n or code in skip or code in seen:
            continue
        seen.add(code)
        picked.append({"key": _slug(code), "label": desc or code, "codes": [code], "aux": []})
        if len(picked) >= RADAR_TOP_N:
            break
    return [RODENT] + picked


# Static fallback for import-time consumers (fetch/build call resolve_signals()).
SIGNALS = [RODENT]

# --- Legacy single-signal aliases (rodent) — kept so any tool still reading the
# old names (and the guardrail's rodent_alerts check) keeps working. ---
SIGNAL_KEY = RODENT["key"]
SIGNAL_LABEL = RODENT["label"]
SERVICE_CODE = RODENT["codes"][0]
AUX_SIGNALS = [
    {"key": a["key"], "label": a["label"], "service_code": (a.get("codes") or [""])[0]}
    for a in RODENT["aux"]
]

# --- Category breakdown (dashboard.html "what are people complaining about?") ---
# Ever-present / non-complaint categories the CityCast piece sets aside: it calls
# trash + parking enforcement perpetual background noise ("it is always trash and
# parking enforcement season in D.C.") and explicitly drops information requests
# ("generally aren't tagged with a location"). The dashboard's default view hides
# these so genuine local outliers surface; a toggle brings them back.
CATEGORY_START_YEAR = 2016      # taxonomy is stable + comparable from here on
CATEGORY_TOP_N = 35            # keep the top-N codes citywide; fold the rest into "Other"
EXCLUDE_CODES = [
    "S0031",       # Bulk Collection
    "S0441",       # Trash Collection - Missed
    "S0321",       # Recycling Collection - Missed
    "S0346",       # Sanitation Enforcement
    "S0261",       # Parking Enforcement
    "RPP",         # Residential Parking Permit Violation
    "S0336",       # Out of State Parking Violation (ROSA)
    "DCGOVTINFO",  # DC Government Information (info requests, not a complaint)
]

# --- Per-area anomaly board (dashboard.html "what's unusual in each ward") ---
# The article's core device, generalized: for each ward, which complaint category
# is running most above that ward's own seasonal (same-month, prior-years) normal.
ANOMALY_START_YEAR = 2019    # months fetched from here; needs a few prior years for a baseline
ANOMALY_BASELINE_YEARS = 3   # prior years of the same calendar month = the "normal"
ANOMALY_MIN_ABS = 20        # min reports in the target month to rank (kills small-number noise)
ANOMALY_TOP_PER_WARD = 8    # standouts kept per ward
ANOMALY_BOARD_N = 20        # citywide (ward, category) standouts on the leaderboard

# --- History window for seasonal baselines ---
START_YEAR = 2018  # earliest year to pull; more history = better seasonality

# --- Spatial resolution ---
# Detection/alerts run at neighborhood-cluster level (stable, human-named).
# H3_RES is only a caching key for point->neighborhood assignment.
H3_RES = 8
HEAT_RES = 9         # fine grid (~0.1 km^2) for the activity heatmap only
HEAT_MIN_TOTAL = 5   # drop heat cells with fewer than this many all-time reports

# --- Detector knobs ---
WOY_WINDOW = 3       # +/- weeks around the target week-of-year for the baseline
MIN_REF_POINTS = 5   # min historical reference weeks needed to score a week
MIN_ABS = 8          # absolute count floor to alert (kills small-number noise)
ALERT_Z = 3.5        # z-score to raise a single-week alert
PERSIST_Z = 2.0      # lower z that, sustained, also counts (persistence)
PERSIST_WEEKS = 3    # consecutive weeks >= PERSIST_Z to fire via persistence

# --- ArcGIS source ---
ARCGIS_SERVICE = (
    "https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_DATA/"
    "ServiceRequests/MapServer"
)
NEIGHBORHOOD_LAYER = (
    "https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_DATA/"
    "Administrative_Other_Boundaries_WebMercator/MapServer/17"
)
