"""Config for the DC 311 early-warning pipeline.

The whole pipeline is parameterized on a single "signal" so the same
detection engine can be pointed at any 311 service type later (potholes,
trash, etc.). Rats are just the first configured signal.
"""

# --- The signal we're analyzing (swap SERVICE_CODE to generalize) ---
SIGNAL_KEY = "rodent"
SIGNAL_LABEL = "Rodent (rat) reports"
SERVICE_CODE = "S0311"  # "Rodent Inspection and Treatment"

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
