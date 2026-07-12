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
import json
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

DEFAULT_OVERRIDES_PATH = "geocode_overrides.csv"
DEFAULT_CACHE_PATH = "geocode_cache.json"

NOMINATIM_USER_AGENT = (
    "auction-scout/1.0 (research use; small nightly batch)"
)

# Census's geocoder doesn't publish a User-Agent policy, but requests sends
# "python-requests/x.x" by default when no headers are set, and generic
# bot-like UAs are a common target for WAF/gateway throttling. Sending a
# real, identifying UA (same convention as NOMINATIM_USER_AGENT) costs
# nothing and rules this out as a cause of the 502s/timeouts the batch
# endpoint has been throwing from scripted calls but not from manual curl
# tests (which send curl's own UA).
CENSUS_USER_AGENT = "auction-scout/1.0 (research use; small nightly batch)"


def _atomic_write_json(obj, path, **json_kwargs):
    """
    Write JSON atomically: serialize to a temp file in the same directory,
    then os.replace() it over the target. os.replace is atomic on both
    POSIX and Windows -- the target path always contains either the fully
    old file or the fully new one, never a partial write. Without this, a
    crash mid-write (e.g. a non-serializable value, Ctrl+C, power loss)
    leaves a half-written file that raises json.JSONDecodeError on the
    next load and can crash the whole run (see base.py's scraped_cache.json
    for a real instance of exactly this).
    """
    path = Path(path)
    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent or ".", prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, **json_kwargs)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _safe_read_json(path, warn_label):
    """
    Load a JSON cache file, tolerating a corrupt/truncated file by warning
    and treating it as empty rather than crashing the whole geocode run --
    a cache is disposable by design, so a parse error here should cost you
    a slower run, never a failed one.
    """
    p = Path(path)
    if not p.exists():
        return {}

    with open(p, encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as e:
            print(f"WARNING: {warn_label} at {path} is corrupt ({e}) -- "
                  f"ignoring it and starting fresh. The old file has been "
                  f"left in place at {path} in case you want to inspect it; "
                  f"it will be overwritten on the next successful save.")
            return {}


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


def load_geocode_cache(path=DEFAULT_CACHE_PATH):
    """
    Load previously resolved address -> (lat, lon) pairs.

    Keyed on the exact address string passed in (e.g. "4600 Silver Hill Rd,
    Washington, DC 20233"), not on listing id -- ids can churn across
    scraper runs (a listing gets re-posted, an auction house renumbers,
    etc.) but the same street address always resolves to the same
    coordinates. Keying on address means a rerun after a crash/timeout
    skips the API entirely for anything already solved, and any new
    listing that happens to reuse a previously-seen address string still
    gets a free hit.

    Only successful matches are ever stored here (see geocode_with_fallbacks) --
    never cache a "no match", since that could just as easily mean a
    transient timeout as a genuine bad address, and caching it would make
    that failure permanent.

    A missing or corrupt file (see _safe_read_json) is treated as empty
    rather than crashing the run.
    """

    raw = _safe_read_json(path, warn_label="geocode cache")
    return {addr: tuple(coords) for addr, coords in raw.items()}


def save_geocode_cache(cache, path=DEFAULT_CACHE_PATH):
    """
    Persist the cache. Called incrementally (after each Census chunk and
    each Nominatim lookup) rather than only at the very end, so progress
    survives a mid-run crash/timeout instead of being lost with it.

    Written atomically (see _atomic_write_json) so a crash partway through
    a save can't leave behind a half-written, unparseable file.
    """

    _atomic_write_json(cache, path)


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
        headers={"User-Agent": CENSUS_USER_AGENT},
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


def chunked(items, size):
    """Yield successive `size`-length chunks from `items`."""
    for i in range(0, len(items), size):
        yield items[i:i + size]


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


def geocode_batch_with_retry(chunk, max_retries=2, backoff_seconds=3):
    """
    Wraps geocode_batch() with a few retries on transient failures (502s,
    read timeouts) before giving up on this chunk. A single 502 or timeout
    is common under Census's normal server strain and often succeeds on a
    second attempt a few seconds later -- retrying here means one bad
    moment doesn't immediately dump 20+ addresses down to the slower/
    less-accurate Nominatim fallback. Backoff increases with each retry
    (3s, 6s, ...) to avoid hitting an already-struggling server again
    right away.

    Raises the last exception if every attempt fails, same as
    geocode_batch() would -- the caller's existing except block handles
    that identically to before.
    """
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return geocode_batch(chunk)
        except requests.RequestException as e:
            last_exc = e
            if attempt < max_retries:
                wait = backoff_seconds * (attempt + 1)
                print(f"  chunk failed ({type(e).__name__}: {e}) -- "
                      f"retrying in {wait}s (attempt {attempt + 2}/{max_retries + 1})")
                time.sleep(wait)
    raise last_exc


def geocode_with_fallbacks(
        addresses,
        overrides_path=DEFAULT_OVERRIDES_PATH,
        nominatim_delay=1.1,
        census_batch_size=20,
        cache_path=DEFAULT_CACHE_PATH,
):
    """
    Geocode a batch of addresses using the fallback chain:

        manual overrides -> local cache -> Census batch (chunked) ->
        Nominatim (one at a time)

    addresses:
        [(id, address), ...]

    census_batch_size:
        Max addresses sent to the Census batch geocoder per request.
        Sending everything in one POST (previously all ~300-400+ at once)
        was tripping the 60s read timeout on the whole batch, which then
        dumped every remaining address down to the slower/less accurate
        Nominatim fallback. Smaller chunks keep each request comfortably
        under the timeout and mean one bad chunk doesn't sacrifice
        addresses in the others.

    cache_path:
        Where resolved address -> (lat, lon) pairs are persisted (see
        load_geocode_cache). This is what saves a rerun (e.g. after a
        Census timeout or a crash partway through) from re-hitting the
        API for every address already solved in a prior run -- only the
        addresses genuinely still unresolved go back out to Census/
        Nominatim.

    returns:
        (coords, still_unmatched)

        coords          { id : (lat, lon) }
        still_unmatched [ (id, address), ... ]   # failed every method
    """

    overrides = load_geocode_overrides(overrides_path)
    cache = load_geocode_cache(cache_path)

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

    # 1.5. Local cache -- addresses already resolved by a prior run. Keyed
    #      on the address string (see load_geocode_cache for why), not id.
    still_needed = []
    for aid, addr in remaining:
        if addr in cache:
            coords[aid] = cache[addr]
        else:
            still_needed.append((aid, addr))
    remaining = still_needed

    if not remaining:
        return coords, []

    # 2. Census batch geocoder, in chunks. Each chunk is wrapped
    #    independently because this endpoint is known to time out / return
    #    inconsistent results under load (no published Census SLA) -- a
    #    failure on one chunk should fall through to per-address Nominatim
    #    for just that chunk's addresses, not crash the whole run or drag
    #    down chunks that would have succeeded fine on their own.
    #
    #    Cache is saved after every chunk (not just at the end) so a
    #    later timeout/crash doesn't cost you the chunks that already
    #    succeeded.
    census_results = {}
    num_chunks = (len(remaining) + census_batch_size - 1) // census_batch_size

    for chunk_num, chunk in enumerate(chunked(remaining, census_batch_size), start=1):
        try:
            chunk_results = geocode_batch_with_retry(chunk)
            census_results.update(chunk_results)

            addr_by_id = dict(chunk)
            for aid, (lat, lon) in chunk_results.items():
                if lat is not None and lon is not None:
                    cache[addr_by_id[aid]] = (lat, lon)
            save_geocode_cache(cache, cache_path)
        except requests.RequestException as e:
            print(f"WARNING: Census batch geocoder failed on chunk "
                  f"{chunk_num}/{num_chunks} ({len(chunk)} addresses) "
                  f"after retries ({type(e).__name__}: {e}) -- these will "
                  f"fall back to Nominatim one address at a time.")

        # Small pause between chunks regardless of outcome -- avoids
        # hammering Census back-to-back if it's already under strain
        # (e.g. right after a chunk that just failed/retried).
        if chunk_num < num_chunks:
            time.sleep(1)

    still_failing = []
    for aid, addr in remaining:
        lat, lon = census_results.get(aid, (None, None))
        if lat is not None and lon is not None:
            coords[aid] = (lat, lon)
        else:
            still_failing.append((aid, addr))

    # 3. Nominatim, one address at a time, with a delay to respect
    #    their usage policy. Cache is saved after each lookup -- these
    #    calls are already rate-limited to roughly 1/sec, so the extra
    #    disk write is negligible overhead, and it means a run that dies
    #    partway through the slow Nominatim fallback doesn't lose that
    #    progress either.
    still_unmatched = []
    for aid, addr in still_failing:
        lat, lon = geocode_nominatim(addr)

        if lat is not None and lon is not None:
            coords[aid] = (lat, lon)
            cache[addr] = (lat, lon)
            save_geocode_cache(cache, cache_path)
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
        resp = requests.get(
            url, headers={"User-Agent": CENSUS_USER_AGENT}, timeout=20
        )
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