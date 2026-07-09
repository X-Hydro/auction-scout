"""
Base spider framework for real-estate auction sites -> CSV of markers.

Design (loosely modeled on scrapy's spider-per-site pattern, but no scrapy
dependency -- just requests/BeautifulSoup, since these are small sites):

  AuctionSpider (base.py)         <- shared plumbing every site needs
      |
      +-- SullivanSpider (spiders/sullivan.py)
      +-- HarmonSpider   (spiders/harmon.py)   <- disabled, see file
      +-- <your next site>.py

Each spider subclass only implements the site-specific parsing:
  - listing_urls():           which page(s) to scrape
  - parse_listing(html):      -> list of raw row dicts
  - parse_detail(html, row):  -> dict of extra fields (optional)

Everything else (robots.txt compliance, rate limiting, date parsing,
geocoding, dedup, CSV writing) lives in the base class so every new
site gets it for free.

Run all registered, robots.txt-permitting spiders:
    python run-scout.py
"""

import csv
import io
import re
import time
import urllib.robotparser
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from requests.utils import requote_uri

DATE_CHUNK_RE = re.compile(
    r"[A-Za-z]{3,9}\.?\s+\d{1,2}(?:,\s*\d{4})?\s+at\s+\d{1,2}(?::\d{2})?\s*[ap]m",
    re.IGNORECASE,
)


def classify_timing(dt, upcoming_window_days=7):
    """Shared by every site's date parser: given a real datetime (or None),
    classify it as 'This Week' / 'Later' / 'Past' / 'Unknown'."""
    if dt is None:
        return "Unknown"
    delta_days = (dt - datetime.now()).total_seconds() / 86400
    if delta_days < 0:
        return "Past"
    if delta_days <= upcoming_window_days:
        return "This Week"
    return "Later"


def parse_auction_date(text, upcoming_window_days=7):
    """
    Parse a scraped date string into a datetime, and classify it as
    'This Week' / 'Later' / 'Past' / 'Unknown'.

    Handles postponed-auction strings that contain TWO dates
    ('Thu. Jul. 9 at 11 am Mon. Aug. 10, 2026 at 1 pm') by taking the
    LAST date chunk, which is always the current/rescheduled one.

    NOTE: this specific parser assumes Sullivan/Harmon-style text dates
    ('Jul. 9 at 11 am'). Sites with a different date format (e.g. numeric
    MM/DD/YYYY) should write their own small extraction function and call
    classify_timing(dt) directly -- see spiders/brockscott.py for an example.
    """
    matches = DATE_CHUNK_RE.findall(text)
    candidate = matches[-1] if matches else text

    try:
        dt = date_parser.parse(candidate, fuzzy=True, default=datetime.now())
    except (ValueError, OverflowError):
        return None, "Unknown"

    return dt, classify_timing(dt, upcoming_window_days)


def clean_url(url):
    """
    Fix hrefs extracted raw from HTML that contain unescaped characters --
    most commonly a literal space in a filename (e.g. a linked PDF named
    "Foreclosure Notice.pdf" on the source site). Browsers silently
    tolerate this when the link is live in a page and gets clicked, but a
    raw space is not a valid URL character -- once extracted as plain text
    into a CSV, different tools handle it differently, and several just
    truncate the URL at the space. requote_uri re-quotes only the parts
    that need it, without double-encoding segments that are already
    properly percent-encoded.
    """
    if not url:
        return url
    return requote_uri(url)


def geocode_batch(addresses):
    """
    Batch geocode via the free US Census Bureau geocoder.
    addresses: list of (id, full_address_string)
    Returns dict: id -> (lat, lon) or (None, None)
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    for aid, addr in addresses:
        writer.writerow([aid, addr, "", "", ""])
    payload = buf.getvalue()

    files = {"addressFile": ("addresses.csv", payload, "text/csv")}
    data = {"benchmark": "Public_AR_Current"}
    resp = requests.post(
        "https://geocoding.geo.census.gov/geocoder/locations/addressbatch",
        files=files, data=data, timeout=60,
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


DEFAULT_OVERRIDES_PATH = "geocode_overrides.csv"

NOMINATIM_USER_AGENT = "auction-scout/1.0 (research use; small nightly batch)"


def load_geocode_overrides(path=DEFAULT_OVERRIDES_PATH):
    """
    Load manually-verified lat/lon corrections, keyed by "source:id" (same
    key format geocode_batch/geocode_with_fallbacks use everywhere else).

    File format (CSV, header required):
        id,latitude,longitude,address,note

    Only id/latitude/longitude are read programmatically -- address/note
    are there purely so a human looking at the file later can tell which
    listing a row is for without decoding the id. A missing file just
    means no overrides exist yet, not an error -- this always returns a
    dict, empty or not.
    """
    overrides = {}
    p = Path(path)
    if not p.exists():
        return overrides

    with open(p, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rid = (row.get("id") or "").strip()
            lat_raw = (row.get("latitude") or "").strip()
            lon_raw = (row.get("longitude") or "").strip()
            if not rid or not lat_raw or not lon_raw:
                continue
            try:
                overrides[rid] = (float(lat_raw), float(lon_raw))
            except ValueError:
                print(f"WARNING: {path}: skipping malformed override row for "
                      f"{rid!r} (latitude={lat_raw!r}, longitude={lon_raw!r})")
    return overrides


def geocode_nominatim(address):
    """
    Single-address fallback geocode via OpenStreetMap Nominatim.

    Only call this for the (usually small) leftover set after the Census
    batch pass fails to match -- never as the primary geocoder. Two
    reasons: Nominatim is one HTTP call per address (no batch endpoint) so
    it's much slower at volume, and Census is the more authoritative
    source for US addresses on the (large majority of) listings it does
    match. Nominatim's usage policy caps free use at 1 request/second and
    requires an identifying User-Agent -- both honored by the caller
    (geocode_with_fallbacks applies the delay; the header is set here).

    Returns (lat, lon) or (None, None) if no match / the request failed.
    """
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": address, "format": "json", "limit": 1, "countrycodes": "us"},
            headers={"User-Agent": NOMINATIM_USER_AGENT},
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json()
        if not results:
            return None, None
        return float(results[0]["lat"]), float(results[0]["lon"])
    except (requests.RequestException, ValueError, KeyError, IndexError) as e:
        print(f"  Nominatim failed for {address!r}: {type(e).__name__}: {e}")
        return None, None


def geocode_with_fallbacks(addresses, overrides_path=DEFAULT_OVERRIDES_PATH,
                           nominatim_delay=1.1):
    """
    Resolve lat/lon for a list of (id, full_address_string) pairs using,
    in order:
      1. Manual overrides file -- always wins, never re-geocoded once a
         human has looked something up. This is the fast path.
      2. Census Bureau batch geocoder (geocode_batch) -- free, good
         coverage for standard US addresses, and a single batched HTTP
         call regardless of volume.
      3. Nominatim, one address at a time -- slower and rate-limited, but
         a genuinely different data source/algorithm, so it catches some
         addresses Census can't match (new construction, unusual unit
         formatting, etc).

    Returns (results, still_unmatched):
      - results: dict of id -> (lat, lon) for every input id (None, None)
        if all three steps failed for that id.
      - still_unmatched: list of (id, address) pairs that need a human to
        look up and add to overrides_path.
    """
    overrides = load_geocode_overrides(overrides_path)

    results = {}
    remaining = []
    for aid, addr in addresses:
        if aid in overrides:
            results[aid] = overrides[aid]
        else:
            remaining.append((aid, addr))

    if overrides:
        applied = sum(1 for aid, _ in addresses if aid in overrides)
        if applied:
            print(f"Applied {applied} manual geocode override(s) from {overrides_path}")

    if remaining:
        print(f"Geocoding {len(remaining)} address(es) via Census batch geocoder...")
        census_results = geocode_batch(remaining)
        for aid, addr in remaining:
            results[aid] = census_results.get(aid, (None, None))

    unmatched_after_census = [
        (aid, addr) for aid, addr in remaining if results.get(aid) == (None, None)
    ]

    if unmatched_after_census:
        print(f"Trying Nominatim fallback for {len(unmatched_after_census)} "
              f"address(es) the Census geocoder couldn't match...")
        for aid, addr in unmatched_after_census:
            lat, lon = geocode_nominatim(addr)
            results[aid] = (lat, lon)
            time.sleep(nominatim_delay)  # Nominatim usage policy: max 1 req/sec

    still_unmatched = [
        (aid, addr) for aid, addr in addresses if results.get(aid) == (None, None)
    ]
    return results, still_unmatched


class RobotsBlocked(Exception):
    """Raised when a spider's target path is disallowed by robots.txt."""


class AuctionSpider(ABC):
    """
    Subclass this per site. Required overrides: name, base_url,
    listing_urls(), parse_listing(). parse_detail() is optional --
    return {} if a site's listing page already has everything you need.
    """

    name = "unnamed"
    base_url = ""  # e.g. "https://example.com"
    skip_statuses = {"cancelled"}
    scrape_details = True
    request_delay = 1.0
    user_agent = "Mozilla/5.0 (compatible; auction-mapper/1.0; +research use)"

    # Default: honor robots.txt, fail closed if it can't be verified.
    # Set to False in a specific spider ONLY when you have actual, direct
    # permission from that site's owner -- and say so in a comment right at
    # the override, since this is meant to be an explicit, visible exception
    # per-site, never a blanket "ignore robots.txt" switch. See
    # spiders/harmon.py for a real example of when/why this was used.
    respect_robots = True

    def __init__(self):
        if not self.respect_robots:
            print(f"[{self.name}] respect_robots=False -- robots.txt check "
                  f"skipped for this spider (see class docstring for why)")
            self._robots = "__bypassed__"  # sentinel, see allowed()
            return

        self._robots = urllib.robotparser.RobotFileParser()
        robots_url = urljoin(self.base_url, "/robots.txt")
        try:
            resp = requests.get(
                robots_url, headers={"User-Agent": self.user_agent}, timeout=15
            )
            if resp.status_code == 200:
                self._robots.parse(resp.text.splitlines())
            elif resp.status_code in (401, 403):
                # robotparser convention: treat as "disallow everything"
                print(f"[{self.name}] {robots_url} returned {resp.status_code} -- "
                      f"treating as disallow-all")
                self._robots = None
            else:
                # 404 and most other statuses conventionally mean "no restrictions"
                print(f"[{self.name}] {robots_url} returned {resp.status_code} -- "
                      f"treating as no restrictions")
                self._robots.parse([])
        except requests.RequestException as e:
            # Fail closed: don't assume permission if robots.txt is unreachable.
            # But DO surface why, since a silent None here is indistinguishable
            # from a real "Disallow: /" and wastes time debugging the wrong thing.
            print(f"[{self.name}] WARNING: couldn't fetch {robots_url}: "
                  f"{type(e).__name__}: {e}")
            print(f"[{self.name}] Treating as disallowed (fail-closed). "
                  f"Verify manually: curl {robots_url}")
            self._robots = None

    # ---- required per-site overrides -----------------------------------

    @abstractmethod
    def listing_urls(self):
        """Return a list of listing page URLs to scrape."""

    @abstractmethod
    def parse_listing(self, html, listing_url):
        """
        Parse one listing page's HTML into a list of row dicts. Each row
        MUST include at minimum: id, url, date_time, status, street,
        city_state, description.
        """

    def parse_detail(self, html, row):
        """Optional: parse an individual auction detail page for extra
        fields (PDF links, parcel id, etc). Return a dict to merge into
        the row. Default: no-op."""
        return {}

    # ---- shared plumbing --------------------------------------------

    def allowed(self, url):
        if self._robots == "__bypassed__":
            return True
        if self._robots is None:
            return False  # fail closed -- couldn't verify permission
        return self._robots.can_fetch(self.user_agent, url)


    def get_soup(self, url):
        if not self.allowed(url):
            raise RobotsBlocked(f"{self.name}: robots.txt disallows {url}")
        resp = requests.get(url, headers={"User-Agent": self.user_agent}, timeout=20)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")

    def scrape(self):
        """Run the full pipeline for this spider: listing -> details ->
        dedupe -> return list of row dicts (NOT yet geocoded)."""
        by_id = {}
        for listing_url in self.listing_urls():
            if not self.allowed(listing_url):
                print(f"[{self.name}] SKIPPED (robots.txt disallows): {listing_url}")
                continue
            print(f"[{self.name}] Fetching listing: {listing_url}")
            soup = self.get_soup(listing_url)
            for row in self.parse_listing(soup, listing_url):
                if row.get("status", "").lower() in self.skip_statuses:
                    continue
                if not row.get("id"):
                    continue
                row["source"] = self.name
                by_id[(self.name, row["id"])] = row  # last one wins (handles postponed dupes)

        rows = list(by_id.values())

        total = len(rows)
        for i, row in enumerate(rows, start=1):
            row["auction_dt"], row["timing"] = parse_auction_date(row["date_time"])

            if self.scrape_details and row.get("url"):
                print(f"[{self.name}] ({i}/{total}) Scraping: {row['url']} "
                      f"-- {row.get('street', '')}, {row.get('city_state', '')}")
                try:
                    detail_soup = self.get_soup(row["url"])
                    row.update(self.parse_detail(detail_soup, row))
                except RobotsBlocked as e:
                    print(f"[{self.name}] {e}")
                except requests.RequestException as e:
                    print(f"[{self.name}] failed on {row['url']}: {e}")
                time.sleep(self.request_delay)

        return rows