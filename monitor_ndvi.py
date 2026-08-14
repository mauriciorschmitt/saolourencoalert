#!/usr/bin/env python3
"""
monitor_ndvi.py
================
Automated NDVI monitoring for the Sao Lourenco Reservoir (Mafra, SC, Brazil).

What it does, every time it runs:
  1. Authenticates with the Copernicus Data Space Ecosystem (Sentinel Hub
     Statistical API) using OAuth2 client credentials.
  2. Requests NDVI statistics for the reservoir ROI for the most recent
     ~30-day window, uses the SCL cloud mask to calculate indices, but does NOT discard the latest scene merely because cloud cover exceeds 10%.
  3. Picks the most recent scene with at least a small fraction of valid pixels.
  4. Compares its NDVI value against the historical monthly climatology
     (mean and SD computed from the pre-outbreak baseline, 2016-2024, in
     the paper), using the same z-score anomaly method used in the article.
  5. Renders a side-by-side image panel (true color | NDVI | NDWI) for that
     same date and geometry (Sentinel Hub Process API).
  6. Sends the numeric result via Telegram and/or email, every run -- with
     the image panel attached. If image rendering fails for any reason, the
     text alert is still sent (the image is best-effort, never blocking).

This is meant to be run on a schedule (see the GitHub Actions workflow in
.github/workflows/ndvi_monitor.yml) -- it is a single-shot script, not a
long-running service.

REQUIRED SETUP BEFORE FIRST RUN
--------------------------------
1. Fill in MONTHLY_CLIMATOLOGY below with the mean/SD values from your
   "Monthly_Climatology" sheet in Salvinia_Outbreak_Statistical_Results.xlsx
   (pre-outbreak period only, one row per calendar month).
2. Fill in ROI_GEOJSON below with your reservoir polygon (convert your KML
   to GeoJSON once, e.g. at https://mygeodata.cloud/converter/kml-to-geojson,
   and paste the "coordinates" array here).
3. Set the following as GitHub Actions secrets (Settings > Secrets and
   variables > Actions) -- never hard-code these in the script:
     SH_CLIENT_ID, SH_CLIENT_SECRET   (Copernicus Sentinel Hub OAuth client)
     TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
     EMAIL_ADDRESS, EMAIL_APP_PASSWORD, ALERT_EMAIL_TO
   See SETUP_GUIDE.md for how to obtain each of these.
"""

import os
import sys
import json
import smtplib
import datetime as dt
from email.mime.text import MIMEText

import requests
from pyproj import Transformer

# ---------------------------------------------------------------------------
# 1. CONFIGURATION -- edit this section
# ---------------------------------------------------------------------------

# Monthly climatology for NDVI, computed from the PRE-OUTBREAK period only
# (same reference used in the paper's anomaly analysis, Section 2.4 / 3.7).
# Copy these 12 values from the "Monthly_Climatology" sheet in your
# Salvinia_Outbreak_Statistical_Results.xlsx file (ndvi_clim_mean, ndvi_clim_sd).
# PLACEHOLDER VALUES BELOW -- REPLACE BEFORE FIRST RUN.
MONTHLY_CLIMATOLOGY = {
    1:  {"mean": 0.062983286, "sd": 0.022422375},
    2:  {"mean": 0.102973448, "sd": 0.056671219},
    3:  {"mean": 0.129464011, "sd": 0.060937651},
    4:  {"mean": 0.131759039, "sd": 0.077113975},
    5:  {"mean": 0.081960490, "sd": 0.097855931},
    6:  {"mean": 0.005082073, "sd": 0.091559002},
    7:  {"mean": 0.058218087, "sd": 0.097758882},
    8:  {"mean": 0.039807745, "sd": 0.090688466},
    9:  {"mean": 0.052855900, "sd": 0.074378438},
    10: {"mean": -0.065839763, "sd": 0.137611379},
    11: {"mean": 0.079915785, "sd": 0.091192902},
    12: {"mean": 0.017970982, "sd": 0.152124149},
}


# Reservoir ROI is stored separately in data/roi.geojson so it is easy to maintain.
ROI_PATH = os.path.join(os.path.dirname(__file__), "data", "roi.geojson")
with open(ROI_PATH, "r", encoding="utf-8") as _f:
    ROI_GEOJSON = json.load(_f)

CLOUD_THRESHOLD = 10          # percent; informational only, NOT a filter
ALERT_Z_THRESHOLD = 2.0       # SD, same as used in the paper (Section 2.4/3.7)
LOOKBACK_DAYS = 30            # how far back to search for the latest Sentinel-2 image
MIN_VALID_FRACTION = 0.01     # accept scenes with very high cloud cover; only reject ~100% masked scenes

# Number of 10m pixels that fall INSIDE the reservoir polygon (not its
# bounding box). The Statistical API on the Copernicus Dataspace Ecosystem
# does not return "geometryPixelCount" in the response, so we can't read
# this back from the API directly -- it's derived instead from:
#   (a) the reservoir's mapped area (~794,900 m^2 in UTM 22S) / 100 m^2 per
#       10x10m pixel =~ 7,949 pixels (theoretical), and
#   (b) the actual max "valid_px" observed across several clear-sky days in
#       this window (8,761 pixels) -- used here since it reflects exactly
#       how Sentinel Hub rasterizes this specific polygon.
# If you ever redraw/replace ROI_GEOJSON, recompute this: temporarily set
# CLOUD_THRESHOLD very high, run with the debug logging on, and take the
# largest "valid_px" seen across a handful of clearly cloud-free dates.
GEOMETRY_PIXEL_COUNT = 8761

SH_TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
SH_STATS_URL = "https://sh.dataspace.copernicus.eu/api/v1/statistics"

# NDVI evalscript (same band math as the paper: (B08 - B04) / (B08 + B04))
# Cloud filtering uses the Scene Classification Layer (SCL), not just the
# generic dataMask -- dataMask alone only means "the sensor has SOME reading
# here" (e.g. inside the swath), it does NOT mean "this pixel is cloud-free".
# SCL classes excluded here: 3 = cloud shadow, 8 = cloud (medium probability),
# 9 = cloud (high probability), 10 = thin cirrus. (Class 11, snow/ice, is left
# in since it's not relevant for this reservoir; add it here if needed.)
EVALSCRIPT_INDICES = """
//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B08", "B04", "B03", "B11", "SCL", "dataMask"] }],
    output: [
      { id: "ndvi", bands: 1, sampleType: "FLOAT32" },
      { id: "ndwi", bands: 1, sampleType: "FLOAT32" },
      { id: "ndmi", bands: 1, sampleType: "FLOAT32" },
      { id: "dataMask", bands: 1 }
    ]
  };
}
function evaluatePixel(s) {
  let isCloud = (s.SCL == 3 || s.SCL == 8 || s.SCL == 9 || s.SCL == 10);
  let valid = s.dataMask == 1 && !isCloud;
  let ndvi = (s.B08 + s.B04) == 0 ? 0 : (s.B08 - s.B04) / (s.B08 + s.B04);
  let ndwi = (s.B03 + s.B08) == 0 ? 0 : (s.B03 - s.B08) / (s.B03 + s.B08);
  // NDMI = (NIR - SWIR1) / (NIR + SWIR1). B11 is 20 m; the API resamples it to 10 m.
  // It is intentionally included below as an input band.
  let ndmi = (s.B08 + s.B11) == 0 ? 0 : (s.B08 - s.B11) / (s.B08 + s.B11);
  return { ndvi: [ndvi], ndwi: [ndwi], ndmi: [ndmi], dataMask: [valid ? 1 : 0] };
}
"""

# ---------------------------------------------------------------------------
# 2a. VISUALIZATION EVALSCRIPTS (for the image panel attached to alerts)
# ---------------------------------------------------------------------------
# These render actual RGB images (Process API), not statistics. Cloud/cloud
# shadow pixels (same SCL classes as EVALSCRIPT_NDVI above) are painted
# light grey so a cloudy scene is visually obvious rather than misleading.

EVALSCRIPT_TRUECOLOR = """
//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B04", "B03", "B02", "SCL", "dataMask"] }],
    output: { bands: 3, sampleType: "AUTO" }
  };
}
function evaluatePixel(s) {
  let isCloud = (s.SCL == 3 || s.SCL == 8 || s.SCL == 9 || s.SCL == 10);
  if (s.dataMask == 0) { return [1, 1, 1]; }
  if (isCloud) { return [0.85, 0.85, 0.85]; }
  // simple gain + gamma, standard true-color stretch for Sentinel-2 L2A
  let gain = 3.0;
  let gamma = 1.8;
  return [
    Math.pow(s.B04 * gain, 1 / gamma),
    Math.pow(s.B03 * gain, 1 / gamma),
    Math.pow(s.B02 * gain, 1 / gamma),
  ];
}
"""

# Shared green<->brown diverging ramp for NDVI: browns/tans for low or
# negative values (open water, bare soil), greens for high values (dense
# vegetation) -- matches the color logic used for Figure 7 in the paper.
_NDVI_COLOR_RAMP_JS = """
function ndviColor(v) {
  // v in [-1, 1]
  if (v < -0.2) return [0.65, 0.55, 0.40];   // water / non-vegetated -> tan
  if (v < 0.0)  return [0.80, 0.75, 0.55];
  if (v < 0.2)  return [0.90, 0.88, 0.55];
  if (v < 0.4)  return [0.65, 0.80, 0.35];
  if (v < 0.6)  return [0.30, 0.65, 0.20];
  return [0.05, 0.45, 0.05];                 // dense vegetation -> dark green
}
"""

EVALSCRIPT_NDVI_VIZ = ("""
//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B08", "B04", "SCL", "dataMask"] }],
    output: { bands: 3, sampleType: "AUTO" }
  };
}
""" + _NDVI_COLOR_RAMP_JS + """
function evaluatePixel(s) {
  let isCloud = (s.SCL == 3 || s.SCL == 8 || s.SCL == 9 || s.SCL == 10);
  if (s.dataMask == 0) { return [1, 1, 1]; }
  if (isCloud) { return [0.85, 0.85, 0.85]; }
  let ndvi = (s.B08 - s.B04) / (s.B08 + s.B04);
  return ndviColor(ndvi);
}
""")

# Blue diverging ramp for NDWI: dark blue for open water (high NDWI), tan
# for vegetated/dry surface (low or negative NDWI).
_NDWI_COLOR_RAMP_JS = """
function ndwiColor(v) {
  // v in [-1, 1]
  if (v < -0.2) return [0.80, 0.75, 0.55];   // vegetated / dry -> tan
  if (v < 0.0)  return [0.85, 0.85, 0.65];
  if (v < 0.2)  return [0.65, 0.80, 0.90];
  if (v < 0.4)  return [0.30, 0.60, 0.85];
  return [0.05, 0.25, 0.65];                 // open water -> dark blue
}
"""

EVALSCRIPT_NDWI_VIZ = ("""
//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B03", "B08", "SCL", "dataMask"] }],
    output: { bands: 3, sampleType: "AUTO" }
  };
}
""" + _NDWI_COLOR_RAMP_JS + """
function evaluatePixel(s) {
  let isCloud = (s.SCL == 3 || s.SCL == 8 || s.SCL == 9 || s.SCL == 10);
  if (s.dataMask == 0) { return [1, 1, 1]; }
  if (isCloud) { return [0.85, 0.85, 0.85]; }
  let ndwi = (s.B03 - s.B08) / (s.B03 + s.B08);
  return ndwiColor(ndwi);
}
""")

SH_PROCESS_URL = "https://sh.dataspace.copernicus.eu/api/v1/process"
PANEL_IMAGE_SIZE = 512   # pixels, per sub-image (longer side); keeps requests small/fast


# ---------------------------------------------------------------------------
# 2. AUTHENTICATION
# ---------------------------------------------------------------------------

def get_access_token():
    client_id = os.environ["SH_CLIENT_ID"]
    client_secret = os.environ["SH_CLIENT_SECRET"]
    resp = requests.post(
        SH_TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(
            f"Token endpoint returned {resp.status_code}: {resp.text}"
        )
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# 2.5 GEOMETRY REPROJECTION (WGS84 -> UTM)
# ---------------------------------------------------------------------------
# The Sentinel Hub Statistical API interprets "resx"/"resy" in the SAME UNITS
# as the request's CRS. ROI_GEOJSON above is in WGS84 (EPSG:4326), i.e.
# degrees of latitude/longitude -- so "resx": 10 would mean a 10-DEGREE pixel
# (larger than the whole state of Santa Catarina), not 10 meters, and the API
# rejects the request. To get real 10 m pixels we reproject the polygon to
# its local UTM zone (in meters) before building the request.

def _iter_coords(coords):
    """Recursively walk a GeoJSON coordinates structure, yielding
    (ring, index, [lon, lat, ...]) tuples for every vertex, regardless of
    nesting depth (Polygon vs MultiPolygon)."""
    if isinstance(coords[0], (int, float)):
        yield coords
    else:
        for item in coords:
            yield from _iter_coords(item)


def reproject_to_utm(geometry):
    """Reproject a GeoJSON Polygon/MultiPolygon geometry (in EPSG:4326) to
    its local UTM zone. Returns (reprojected_geometry, epsg_code)."""
    all_points = list(_iter_coords(geometry["coordinates"]))
    lon_c = sum(p[0] for p in all_points) / len(all_points)
    lat_c = sum(p[1] for p in all_points) / len(all_points)

    zone = int((lon_c + 180) // 6) + 1
    epsg = (32700 if lat_c < 0 else 32600) + zone  # UTM south/north zone

    transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)

    def _transform(coords):
        if isinstance(coords[0], (int, float)):
            x, y = transformer.transform(coords[0], coords[1])
            return [x, y]
        return [_transform(c) for c in coords]

    reprojected = {
        "type": geometry["type"],
        "coordinates": _transform(geometry["coordinates"]),
    }
    return reprojected, epsg


# ---------------------------------------------------------------------------
# 3. FETCH LATEST NDVI STATISTICS
# ---------------------------------------------------------------------------

def _stat_mean(item, output_id):
    return item.get("outputs", {}).get(output_id, {}).get("bands", {}).get("B0", {}).get("stats")


def fetch_latest_metrics(token):
    today = dt.date.today()
    start = today - dt.timedelta(days=LOOKBACK_DAYS)
    roi_geometry_wgs84 = ROI_GEOJSON["features"][0]["geometry"]
    roi_geometry_utm, utm_epsg = reproject_to_utm(roi_geometry_wgs84)

    payload = {
        "input": {
            "bounds": {
                "geometry": roi_geometry_utm,
                "properties": {"crs": f"http://www.opengis.net/def/crs/EPSG/0/{utm_epsg}"},
            },
            "data": [{"type": "sentinel-2-l2a"}],
        },
        "aggregation": {
            "timeRange": {
                "from": f"{start.isoformat()}T00:00:00Z",
                "to": f"{today.isoformat()}T23:59:59Z",
            },
            "aggregationInterval": {"of": "P1D"},
            "resx": 10,
            "resy": 10,
            "evalscript": EVALSCRIPT_INDICES,
        },
        "calculations": {
            "ndvi": {"statistics": {"default": {}}},
            "ndwi": {"statistics": {"default": {}}},
            "ndmi": {"statistics": {"default": {}}},
        },
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = requests.post(SH_STATS_URL, headers=headers, json=payload, timeout=90)
    if not resp.ok:
        raise RuntimeError(f"Statistical API returned {resp.status_code}: {resp.text}")
    data = resp.json()

    candidates = []
    for item in data.get("data", []):
        ndvi_stats = _stat_mean(item, "ndvi")
        ndwi_stats = _stat_mean(item, "ndwi")
        ndmi_stats = _stat_mean(item, "ndmi")
        if not ndvi_stats or not ndwi_stats or not ndmi_stats:
            continue
        sample_count = ndvi_stats.get("sampleCount", 0)
        if sample_count <= 0:
            continue
        valid_px = sample_count - ndvi_stats.get("noDataCount", 0)
        valid_fraction = valid_px / max(GEOMETRY_PIXEL_COUNT, 1)
        if valid_fraction < MIN_VALID_FRACTION:
            continue
        date_str = item["interval"]["from"][:10]
        cloud_pct = max(0.0, min(100.0, (1.0 - valid_fraction) * 100.0))
        candidates.append({
            "date": date_str,
            "ndvi": ndvi_stats.get("mean"),
            "ndwi": ndwi_stats.get("mean"),
            "ndmi": ndmi_stats.get("mean"),
            "cloud_pct": cloud_pct,
            "valid_fraction": valid_fraction,
        })

    if not candidates:
        return None
    candidates.sort(key=lambda x: x["date"])
    return candidates[-1]


# ---------------------------------------------------------------------------
# 3.5 IMAGE PANEL (true color + NDVI + NDWI, side by side)
# ---------------------------------------------------------------------------

def _bbox_from_geometry(geometry_wgs84, pad_fraction=0.05):
    """Return [min_lon, min_lat, max_lon, max_lat] for a GeoJSON geometry,
    padded a little so the reservoir isn't flush against the image edge."""
    points = list(_iter_coords(geometry_wgs84["coordinates"]))
    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    min_lon, max_lon = min(lons), max(lons)
    min_lat, max_lat = min(lats), max(lats)
    pad_lon = (max_lon - min_lon) * pad_fraction
    pad_lat = (max_lat - min_lat) * pad_fraction
    return [min_lon - pad_lon, min_lat - pad_lat, max_lon + pad_lon, max_lat + pad_lat]


def _fetch_process_image(token, evalscript, bbox_wgs84, date_str, width, height):
    """Single Process API call -> raw PNG bytes for one evalscript/date."""
    payload = {
        "input": {
            "bounds": {
                "bbox": bbox_wgs84,
                "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"},
            },
            "data": [{
                "type": "sentinel-2-l2a",
                "dataFilter": {
                    "timeRange": {
                        "from": f"{date_str}T00:00:00Z",
                        "to": f"{date_str}T23:59:59Z",
                    },
                    "mosaickingOrder": "leastCC",
                },
            }],
        },
        "output": {
            "width": width,
            "height": height,
            "responses": [{"identifier": "default", "format": {"type": "image/png"}}],
        },
        "evalscript": evalscript,
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = requests.post(SH_PROCESS_URL, headers=headers, json=payload, timeout=60)
    if not resp.ok:
        raise RuntimeError(f"Process API returned {resp.status_code}: {resp.text[:500]}")
    return resp.content  # raw PNG bytes


def fetch_image_panel(token, date_str, roi_geometry_wgs84):
    """Build a single side-by-side PNG (true color | NDVI | NDWI) for the
    given date and reservoir geometry. Returns PNG bytes, or None if image
    rendering fails for any reason (an image is a nice-to-have -- a failure
    here should never block the numeric alert itself)."""
    from PIL import Image, ImageDraw, ImageFont
    import io

    try:
        bbox = _bbox_from_geometry(roi_geometry_wgs84)
        lon_span = bbox[2] - bbox[0]
        lat_span = bbox[3] - bbox[1]
        # keep sub-images at a sensible aspect ratio instead of a fixed square
        if lon_span >= lat_span:
            width = PANEL_IMAGE_SIZE
            height = max(64, int(PANEL_IMAGE_SIZE * lat_span / lon_span))
        else:
            height = PANEL_IMAGE_SIZE
            width = max(64, int(PANEL_IMAGE_SIZE * lon_span / lat_span))

        specs = [
            ("True color", EVALSCRIPT_TRUECOLOR),
            ("NDVI", EVALSCRIPT_NDVI_VIZ),
            ("NDWI", EVALSCRIPT_NDWI_VIZ),
        ]

        tiles = []
        for label, script in specs:
            png_bytes = _fetch_process_image(token, script, bbox, date_str, width, height)
            tiles.append((label, Image.open(io.BytesIO(png_bytes)).convert("RGB")))

        label_h = 28
        gap = 6
        panel_w = width * 3 + gap * 2
        panel_h = height + label_h
        panel = Image.new("RGB", (panel_w, panel_h), "white")
        draw = ImageDraw.Draw(panel)
        try:
            font = ImageFont.load_default()
        except Exception:
            font = None

        x = 0
        for label, tile in tiles:
            panel.paste(tile, (x, label_h))
            draw.text((x + 4, 6), f"{label} - {date_str}", fill="black", font=font)
            x += width + gap

        buf = io.BytesIO()
        panel.save(buf, format="PNG")
        return buf.getvalue()

    except Exception as exc:  # noqa: BLE001
        print(f"[warn] Could not build image panel ({exc}); continuing without it.")
        return None



# ---------------------------------------------------------------------------
# 3.8 PERSIST RESULTS FOR THE PUBLIC DASHBOARD
# ---------------------------------------------------------------------------

PROJECT_ROOT = os.path.dirname(__file__)
DOCS_DIR = os.path.join(PROJECT_ROOT, "docs")
DATA_DIR = os.path.join(DOCS_DIR, "data")
LATEST_IMAGE_DIR = os.path.join(DOCS_DIR, "images", "latest")
ARCHIVE_IMAGE_DIR = os.path.join(DOCS_DIR, "images", "archive")
HISTORY_CSV = os.path.join(DATA_DIR, "historico.csv")
LATEST_JSON = os.path.join(DATA_DIR, "ultimo.json")


def _status_from_z(z):
    # Only the HIGH-NDVI direction matters for this system: an unusually
    # LOW NDVI just means the reservoir is clearer than typical for the
    # month, which is not a macrophyte-bloom signal and should not alert.
    if z >= ALERT_Z_THRESHOLD:
        return "ALERTA"
    if z >= 1.5:
        return "ATENÇÃO"
    return "NORMAL"


def save_dashboard_data(result, z):
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LATEST_IMAGE_DIR, exist_ok=True)
    os.makedirs(ARCHIVE_IMAGE_DIR, exist_ok=True)
    status = _status_from_z(z)
    payload = {
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "date": result["date"],
        "ndvi": result["ndvi"],
        "ndwi": result["ndwi"],
        "ndmi": result["ndmi"],
        "cloud_pct": result["cloud_pct"],
        "valid_fraction": result["valid_fraction"],
        "zscore": z,
        "status": status,
        "cloud_threshold": CLOUD_THRESHOLD,
        "cloud_note": "Cobertura estimada pela máscara SCL na área de estudo; 10% é apenas referência e não bloqueia o envio.",
    }
    with open(LATEST_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    import csv
    fields = ["date", "ndvi", "ndwi", "ndmi", "cloud_pct", "valid_fraction", "zscore", "status"]
    existing = []
    if os.path.exists(HISTORY_CSV):
        with open(HISTORY_CSV, "r", encoding="utf-8", newline="") as f:
            existing = list(csv.DictReader(f))
    row = {k: payload[k] for k in fields}
    by_date = {r.get("date"): r for r in existing}
    by_date[result["date"]] = row
    with open(HISTORY_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for date_key in sorted(by_date):
            writer.writerow(by_date[date_key])

    return payload


def save_panel(panel_bytes, date_str):
    if not panel_bytes:
        return None
    latest_path = os.path.join(LATEST_IMAGE_DIR, "panel.png")
    archive_dir = os.path.join(ARCHIVE_IMAGE_DIR, date_str)
    os.makedirs(archive_dir, exist_ok=True)
    archive_path = os.path.join(archive_dir, "panel.png")
    with open(latest_path, "wb") as f:
        f.write(panel_bytes)
    with open(archive_path, "wb") as f:
        f.write(panel_bytes)
    return latest_path

# ---------------------------------------------------------------------------
# 4. ANOMALY CHECK
# ---------------------------------------------------------------------------

def check_anomaly(date_str, ndvi_value):
    month = int(date_str.split("-")[1])
    clim = MONTHLY_CLIMATOLOGY[month]
    if clim["mean"] is None or clim["sd"] is None:
        raise RuntimeError(
            f"MONTHLY_CLIMATOLOGY for month {month} is not filled in. "
            "Copy the values from your Monthly_Climatology results sheet."
        )
    z = (ndvi_value - clim["mean"]) / clim["sd"]
    return z, z >= ALERT_Z_THRESHOLD


# ---------------------------------------------------------------------------
# 5. ALERTS
# ---------------------------------------------------------------------------

def send_telegram(message, image_bytes=None):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_ids_raw = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_ids_raw:
        print("Telegram not configured, skipping.")
        return
    # TELEGRAM_CHAT_ID can be a single id ("123456789") or a comma-separated
    # list ("123456789,987654321") to notify multiple people/chats. Each
    # person must have started a chat with the bot at least once -- Telegram
    # doesn't allow bots to message someone who hasn't done that first.
    chat_ids = [c.strip() for c in chat_ids_raw.split(",") if c.strip()]

    if image_bytes:
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        # Telegram caption max length is 1024 chars; the message is short
        # enough here, but truncate defensively so the send never fails
        # solely because of caption length.
        caption = message if len(message) <= 1024 else message[:1021] + "..."
        for chat_id in chat_ids:
            files = {"photo": ("panel.png", image_bytes, "image/png")}
            resp = requests.post(
                url, data={"chat_id": chat_id, "caption": caption}, files=files, timeout=60
            )
            if not resp.ok:
                print(f"Telegram photo send to {chat_id} failed: {resp.status_code} {resp.text}")
    else:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        for chat_id in chat_ids:
            resp = requests.post(url, data={"chat_id": chat_id, "text": message}, timeout=30)
            if not resp.ok:
                print(f"Telegram send to {chat_id} failed: {resp.status_code} {resp.text}")


def send_email(subject, message, image_bytes=None):
    addr = os.environ.get("EMAIL_ADDRESS")
    app_password = os.environ.get("EMAIL_APP_PASSWORD")
    to_raw = os.environ.get("ALERT_EMAIL_TO")
    if not addr or not app_password or not to_raw:
        print("Email not configured, skipping.")
        return
    # ALERT_EMAIL_TO can be a single address or a comma-separated list, e.g.
    # "alice@example.com, bob@example.com"
    to_addrs = [a.strip() for a in to_raw.split(",") if a.strip()]

    if image_bytes:
        from email.mime.multipart import MIMEMultipart
        from email.mime.image import MIMEImage
        msg = MIMEMultipart()
        msg["Subject"] = subject
        msg["From"] = addr
        msg["To"] = ", ".join(to_addrs)
        msg.attach(MIMEText(message))
        img = MIMEImage(image_bytes, _subtype="png")
        img.add_header("Content-Disposition", "attachment", filename="reservoir_panel.png")
        msg.attach(img)
    else:
        msg = MIMEText(message)
        msg["Subject"] = subject
        msg["From"] = addr
        msg["To"] = ", ".join(to_addrs)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(addr, app_password)
        server.sendmail(addr, to_addrs, msg.as_string())


# ---------------------------------------------------------------------------
# 6. MAIN
# ---------------------------------------------------------------------------

def main():
    token = get_access_token()
    result = fetch_latest_metrics(token)

    if result is None:
        message = (
            f"[Sao Lourenco Reservoir] Nenhuma imagem Sentinel-2 com pixels válidos "
            f"foi encontrada nos últimos {LOOKBACK_DAYS} dias."
        )
        print(message)
        send_telegram(message)
        send_email("Monitoramento São Lourenço - sem imagem", message)
        return

    z, is_alert = check_anomaly(result["date"], result["ndvi"])
    payload = save_dashboard_data(result, z)
    status = payload["status"]

    message = (
        "🛰️ Monitoramento São Lourenço\n"
        f"📅 Data da imagem: {result['date']}\n"
        f"🌿 NDVI: {result['ndvi']:.3f}\n"
        f"💧 NDWI: {result['ndwi']:.3f}\n"
        f"🌱 NDMI: {result['ndmi']:.3f}\n"
        f"☁️ Nuvens: {result['cloud_pct']:.1f}%\n"
        f"📊 Z-score: {z:.2f}\n"
        f"Status: {status}\n"
    )
    if result["cloud_pct"] > CLOUD_THRESHOLD:
        message += "☁️ Observação: cobertura acima de 10%; a imagem foi mantida no monitoramento e não foi descartada.\n"
    if is_alert:
        message += "⚠️ Anomalia estatística detectada; recomenda-se verificação de campo.\n"

    print(message)
    roi_geometry_wgs84 = ROI_GEOJSON["features"][0]["geometry"]
    panel_bytes = fetch_image_panel(token, result["date"], roi_geometry_wgs84)
    save_panel(panel_bytes, result["date"])

    send_telegram(message, image_bytes=panel_bytes)
    send_email(f"Monitoramento São Lourenço - {status}", message, image_bytes=panel_bytes)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        # Make failures visible in the GitHub Actions log and, optionally,
        # notify by Telegram so silent failures don't go unnoticed.
        err_msg = f"NDVI monitor run failed: {exc}"
        print(err_msg, file=sys.stderr)
        try:
            send_telegram(f"[ERROR] {err_msg}")
        except Exception:
            pass
        sys.exit(1)
