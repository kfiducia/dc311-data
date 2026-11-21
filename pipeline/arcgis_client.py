"""Shared DC ArcGIS REST client: retrying GET + year->layer map. One copy so
retry/error-body fixes land once (DCD19)."""
import json
import sys
import time
import urllib.parse
import urllib.request

import config as C


def get(url, params):
    q = urllib.parse.urlencode(params)
    for attempt in range(6):
        try:
            with urllib.request.urlopen(f"{url}?{q}", timeout=120) as r:
                data = json.load(r)
            # ArcGIS reports failures as HTTP 200 + an "error" body with no
            # features; the pager would read that as end-of-data and rename a
            # truncated .part to final — and since source_counts.json then records
            # the live count, that year would never be re-fetched (DCD16).
            if "error" in data:
                raise RuntimeError(f"ArcGIS error: {data['error']}")
            return data
        except Exception as e:  # noqa: BLE001 - simple retry
            if attempt == 5:
                raise
            print(f"  retry {attempt+1} ({e})", file=sys.stderr)
            time.sleep(3 * (attempt + 1))


def year_layers(service=None):
    """Map each year -> layer id by parsing the MapServer's layer list."""
    service = service or C.ARCGIS_SERVICE
    meta = get(service, {"f": "json"})
    out = {}
    for lyr in meta["layers"]:
        name = lyr["name"]  # e.g. "All Service Requests - 2025"
        if name.startswith("All Service Requests - "):
            tail = name.rsplit("-", 1)[-1].strip()
            if tail.isdigit():
                out[int(tail)] = lyr["id"]
    return out
