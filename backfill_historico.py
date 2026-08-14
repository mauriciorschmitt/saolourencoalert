#!/usr/bin/env python3
"""
backfill_historico.py
======================
ONE-TIME script to populate docs/data/historico.csv with the full historical
NDVI/NDWI/NDMI time series (not just what accumulates from future weekly
runs). Run this once locally or via a manual GitHub Actions dispatch, commit
the result, and you're done -- the regular monitor_ndvi.py keeps appending
to the same file going forward.

It deliberately does NOT:
  - send any Telegram/e-mail notifications (would spam one message per
    historical date);
  - fetch/save the true-color+NDVI+NDWI image panel for every historical
    date (that's a lot of Process API calls for no real benefit -- only
    the *current* panel matters for the dashboard's "latest image" card).

It reuses every bit of query/auth/reprojection logic from monitor_ndvi.py
so the historical series is computed with EXACTLY the same method as the
weekly runs (same evalscript, same cloud masking, same UTM reprojection,
same GEOMETRY_PIXEL_COUNT).

USAGE
-----
    python backfill_historico.py [--since 2016-07-19] [--until 2026-08-01]

Defaults to --since 2016-07-19 (the start of the Sentinel-2 mission's data
record) and --until today.
"""

import argparse
import csv
import datetime as dt
import os
import sys
import time

# Reuses everything from the main script -- single source of truth.
from monitor_ndvi import (
    ROI_GEOJSON,
    GEOMETRY_PIXEL_COUNT,
    MIN_VALID_FRACTION,
    EVALSCRIPT_INDICES,
    SH_STATS_URL,
    get_access_token,
    reproject_to_utm,
    check_anomaly,
    _status_from_z,
    DATA_DIR,
    HISTORY_CSV,
)
import requests

CHUNK_DAYS = 90  # keep each API request's date range modest


def fetch_chunk(token, start, end):
    """One Statistical API call for a bounded date range. Returns a list of
    result dicts, same shape as monitor_ndvi.fetch_latest_metrics()'s
    candidates list."""
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
                "to": f"{end.isoformat()}T23:59:59Z",
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
    resp = requests.post(SH_STATS_URL, headers=headers, json=payload, timeout=120)
    if not resp.ok:
        raise RuntimeError(f"Statistical API returned {resp.status_code}: {resp.text}")
    data = resp.json()

    def stat(item, output_id):
        return item.get("outputs", {}).get(output_id, {}).get("bands", {}).get("B0", {}).get("stats")

    out = []
    for item in data.get("data", []):
        ndvi_stats = stat(item, "ndvi")
        ndwi_stats = stat(item, "ndwi")
        ndmi_stats = stat(item, "ndmi")
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
        out.append({
            "date": date_str,
            "ndvi": ndvi_stats.get("mean"),
            "ndwi": ndwi_stats.get("mean"),
            "ndmi": ndmi_stats.get("mean"),
            "cloud_pct": cloud_pct,
            "valid_fraction": valid_fraction,
        })
    return out


def daterange_chunks(since, until, chunk_days):
    cur = since
    while cur <= until:
        chunk_end = min(cur + dt.timedelta(days=chunk_days - 1), until)
        yield cur, chunk_end
        cur = chunk_end + dt.timedelta(days=1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="2016-07-19")
    parser.add_argument("--until", default=dt.date.today().isoformat())
    args = parser.parse_args()

    since = dt.date.fromisoformat(args.since)
    until = dt.date.fromisoformat(args.until)

    print(f"Backfilling {since} -> {until} in {CHUNK_DAYS}-day chunks...")
    token = get_access_token()

    all_results = []
    failed_chunks = []
    for start, end in daterange_chunks(since, until, CHUNK_DAYS):
        print(f"  fetching {start} .. {end}")
        try:
            chunk = fetch_chunk(token, start, end)
        except Exception as exc:  # noqa: BLE001
            # A single bad chunk (timeout, transient 5xx, etc.) shouldn't
            # kill a ~10-year backfill; log it and keep going, then report
            # the failures at the end so they can be re-run individually.
            print(f"    FAILED: {exc}")
            failed_chunks.append((start, end))
            continue
        print(f"    {len(chunk)} valid scene(s)")
        all_results.extend(chunk)
        time.sleep(1)  # be polite to the API across ~40 sequential requests

    if failed_chunks:
        print(f"\n{len(failed_chunks)} chunk(s) failed and were skipped:")
        for s, e in failed_chunks:
            print(f"  python backfill_historico.py --since {s} --until {e}")
        print("(re-run those ranges individually if you want complete coverage)\n")

    if not all_results:
        print("No valid scenes found in the requested range. Nothing written.")
        sys.exit(0)

    os.makedirs(DATA_DIR, exist_ok=True)

    fields = ["date", "ndvi", "ndwi", "ndmi", "cloud_pct", "valid_fraction", "zscore", "status"]

    # merge with whatever is already in historico.csv (e.g. rows written by
    # normal weekly runs since this backfill's --until date), keyed by date
    # so re-running this script is safe / idempotent
    existing = []
    if os.path.exists(HISTORY_CSV):
        with open(HISTORY_CSV, "r", encoding="utf-8", newline="") as f:
            existing = list(csv.DictReader(f))
    by_date = {r["date"]: r for r in existing if r.get("date")}

    for result in all_results:
        z, _ = check_anomaly(result["date"], result["ndvi"])
        status = _status_from_z(z)
        by_date[result["date"]] = {
            "date": result["date"],
            "ndvi": result["ndvi"],
            "ndwi": result["ndwi"],
            "ndmi": result["ndmi"],
            "cloud_pct": result["cloud_pct"],
            "valid_fraction": result["valid_fraction"],
            "zscore": z,
            "status": status,
        }

    with open(HISTORY_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for date_key in sorted(by_date):
            writer.writerow(by_date[date_key])

    print(f"Done. {len(by_date)} total rows written to {HISTORY_CSV}")
    print(
        "Note: docs/data/ultimo.json and the latest image panel were NOT "
        "touched by this script -- run the normal monitor_ndvi.py once "
        "(or wait for the next scheduled run) to populate those with the "
        "most recent scene."
    )


if __name__ == "__main__":
    main()
