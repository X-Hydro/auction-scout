"""
Geocoding and geographic enrichment utilities.

Provides:

    geocode_batch()              Address -> (lat, lon)
    geocode_nominatim()          Address -> (lat, lon)
    geocode_with_fallbacks()     Address -> (lat, lon)
    reverse_geocode_geography()  (lat, lon) -> political geography
"""

import csv
import io
import re
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

DEFAULT_OVERRIDES_PATH = "geocode_overrides.csv"

NOMINATIM_USER_AGENT = (
    "auction-scout/1.0 (research use; small nightly batch)"
)


def load_geocode_overrides(path=DEFAULT_OVERRIDES_PATH):
    """
    Load manually verified coordinate overrides.

    CSV format:

        id,latitude,longitude,address,note
    """

    overrides = {}

    p = Path(path)
    if not p.exists():
        return overrides

    with open(p, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for row in reader:
            rid = (row.get("id") or "").strip()
            lat = (row.get("latitude") or "").strip()
            lon = (row.get("longitude") or "").strip()

            if not rid or not lat or not lon:
                continue

            try:
                overrides[rid] = (float(lat), float(lon))
            except ValueError:
                print(f"WARNING: bad override row for {rid}")

    return overrides


def geocode_batch(addresses):
    """
    Batch geocode addresses using the US Census geocoder.

    addresses:
        [(id, address), ...]

    returns

        { id : (lat, lon) }
    """

    buf = io.StringIO()
    writer = csv.writer(buf)

    for aid, addr in addresses:
        writer.writerow([aid, addr, "", "", ""])

    payload = buf.getvalue()

    files = {
        "addressFile": ("addresses.csv", payload, "text/csv")
    }

    data = {
        "benchmark": "Public_AR_Current"
    }

    resp = requests.post(
        "https://geocoding.geo.census.gov/geocoder/locations/addressbatch",
        files=files,
        data=data,
        timeout=60,
    )

    resp.raise_for_status()

    results = {}

    for line in resp.text.splitlines():
        parts = next(csv.reader([line]))

        rid = parts[0]
        matched = parts[2] == "Match"

        if matched:
            lon, lat = parts[5].split(",")
            results[rid] = (float(lat), float(lon))
        else:
            results[rid] = (None, None)

    return results


def geocode_nominatim(address):
    """
    Single-address fallback geocoder.
    """

    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": address,
                "format": "json",
                "limit": 1,
                "countrycodes": "us",
            },
            headers={"User-Agent": NOMINATIM_USER_AGENT},
            timeout=15,
        )

        resp.raise_for_status()

        results = resp.json()

        if not results:
            return None, None

        return (
            float(results[0]["lat"]),
            float(results[0]["lon"]),
        )

    except Exception:
        return None, None


def geocode_with_fallbacks(
        addresses,
        overrides_path=DEFAULT_OVERRIDES_PATH,
        nominatim_delay=1.1,
):
    """
    Geocode a batch of addresses using the fallback chain:

        manual overrides -> Census batch -> Nominatim (one at a time)

    addresses:
        [(id, address), ...]

    returns:
        (coords, still_unmatched)

        coords          { id : (lat, lon) }
        still_unmatched [ (id, address), ... ]   # failed every method
    """

    overrides = load_geocode_overrides(overrides_path)

    coords = {}
    remaining = []

    # 1. Manual overrides -- skip these entirely, no need to hit any API.
    for aid, addr in addresses:
        if aid in overrides:
            coords[aid] = overrides[aid]
        else:
            remaining.append((aid, addr))

    if not remaining:
        return coords, []

    # 2. Census batch geocoder. Wrapped because this endpoint is known to
    #    time out / return inconsistent results under load (no published
    #    Census SLA) -- a failure here should fall through to per-address
    #    Nominatim below, not crash the whole run and lose every listing
    #    already scraped before this point.
    try:
        census_results = geocode_batch(remaining)
    except requests.RequestException as e:
        print(f"WARNING: Census batch geocoder failed ({type(e).__name__}: {e}) "
              f"-- falling back to Nominatim one address at a time for all "
              f"{len(remaining)} remaining address(es). This will be slower.")
        census_results = {}

    still_failing = []
    for aid, addr in remaining:
        lat, lon = census_results.get(aid, (None, None))
        if lat is not None and lon is not None:
            coords[aid] = (lat, lon)
        else:
            still_failing.append((aid, addr))

    # 3. Nominatim, one address at a time, with a delay to respect
    #    their usage policy.
    still_unmatched = []
    for aid, addr in still_failing:
        lat, lon = geocode_nominatim(addr)

        if lat is not None and lon is not None:
            coords[aid] = (lat, lon)
        else:
            still_unmatched.append((aid, addr))

        time.sleep(nominatim_delay)

    return coords, still_unmatched


def reverse_geocode_geography(lat, lon):
    """
    Look up authoritative political geography from the US Census.

    Returns:

        {
            "state": "MA",
            "county": "Norfolk County",
            "municipality": "Cohasset",
        }

    Empty strings are returned if a lookup fails.
    """

    url = (
            "https://geocoding.geo.census.gov/geocoder/geographies/coordinates?"
            + urlencode(
        {
            "x": lon,
            "y": lat,
            "benchmark": "Public_AR_Current",
            "vintage": "Current_Current",
            "format": "json",
        }
    )
    )

    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()

        geographies = resp.json()["result"]["geographies"]

        result = {
            "state": "",
            "county": "",
            "municipality": "",
        }

        #
        # State
        #
        states = geographies.get("States", [])
        if states:
            result["state"] = states[0].get("STUSAB", "")

        #
        # County
        #
        counties = geographies.get("Counties", [])
        if counties:
            county_name = counties[0].get("NAME", "")
            #if it ends with County, Stripe the county... So Middlesex County becomes Middlesex
            result["county"] = re.sub(r"\s+County$", "", county_name, flags=re.IGNORECASE)

        #
        # Municipality
        #
        subdivisions = geographies.get("County Subdivisions", [])

        if subdivisions:
            municipality = subdivisions[0].get("NAME", "")

            municipality = re.sub(
                r"\s+(town|city|borough|village|plantation)$",
                "",
                municipality,
                flags=re.IGNORECASE,
            )

            result["municipality"] = municipality

        return result

    except Exception as e:
        print(
            f"WARNING: geography lookup failed "
            f"({lat:.6f}, {lon:.6f}): {e}"
        )

        return {
            "state": "",
            "county": "",
            "municipality": "",
        }