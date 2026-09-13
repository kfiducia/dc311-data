"""Config for the DC 311 early-warning pipeline.

The detection engine is parameterized on a list of **signals** so the same
seasonal-aberration detector can be pointed at any 311 service type. Rats were
the first configured signal; DCD6 generalizes the radar to the categories the
CityCast "what did your neighbors complain about" analysis highlighted — DMV
issues, dockless-vehicle parking, and trash-can repair — each a selectable
signal on early-warning.html.

Each signal:
  key    short slug -> data/raw/<key>_reports.csv and agg/<key>_alerts.json
  label  human name shown in the dashboard's signal picker
  codes  list of ArcGIS SERVICECODE values that make up the signal, OR
  where  an explicit ArcGIS WHERE clause (used instead of `codes` when a signal
         spans a whole family of codes, e.g. every DMV* code)
  aux    corroborating signals fetched + overlaid on the timeline (not detected
         on) — same {key,label,codes|where} shape.
"""

# --- Signals the radar detects on (first = default shown by early-warning.html) ---
SIGNALS = [
    {
        "key": "rodent",
        "label": "Rats & rodents",
        "codes": ["S0311"],  # "Rodent Inspection and Treatment" (a.k.a. Health R&V Control)
        # Dead-animal pickups track rat activity (poisoning die-off, carcasses).
        "aux": [{"key": "dead_animal", "label": "Dead-animal pickups", "codes": ["11"]}],
    },
    {
        "key": "dmv",
        "label": "DMV — licenses, IDs & tickets",
        # The whole DMV* family: driver's-license/ID issues, ticket copies, etc.
        # (the Navy Yard spike the article dug into).
        "where": "SERVICECODE LIKE 'DMV%'",
        "aux": [],
    },
    {
        "key": "dockless",
        "label": "Dockless e-bike / scooter parking",
        "codes": ["DOCVEH2022"],  # "Dockless Vehicle Parking Complaint" (data starts 2022)
        "aux": [],
    },
    {
        "key": "trashcan",
        "label": "Trash-can repair",
        "codes": ["TRACO001"],  # "Trash Cart Repair" (the Ward 5 story)
        "aux": [],
    },
]


def signal_where(sig):
    """ArcGIS WHERE clause for a signal (explicit `where` wins over `codes`)."""
    if sig.get("where"):
        return sig["where"]
    codes = sig.get("codes") or []
    quoted = ",".join("'" + c.replace("'", "''") + "'" for c in codes)
    return f"SERVICECODE IN ({quoted})"


# --- Legacy single-signal aliases (rodent) — kept so any tool still reading the
# old names (and the guardrail's rodent_alerts check) keeps working. ---
SIGNAL_KEY = SIGNALS[0]["key"]
SIGNAL_LABEL = SIGNALS[0]["label"]
SERVICE_CODE = SIGNALS[0]["codes"][0]
AUX_SIGNALS = [
    {"key": a["key"], "label": a["label"], "service_code": (a.get("codes") or [""])[0]}
    for a in SIGNALS[0]["aux"]
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
