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
import json
import os
import re
import tempfile
import time
import urllib.robotparser
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
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


def _atomic_write_json(obj, path, **json_kwargs):
    """
    Write JSON atomically: serialize to a temp file in the same directory,
    then os.replace() it over the target. os.replace is atomic on both
    POSIX and Windows -- the target path always contains either the fully
    old file or the fully new one, never a partial write. This is what
    save_scraped_cache/save_geocode_cache actually need: the bug that
    corrupted scraped_cache.json wasn't the datetime TypeError itself, it
    was that a mid-write crash left a half-written JSON file behind
    because the previous code wrote directly to the target with mode "w"
    (which truncates first). A crash mid-serialize -- this one, a future
    bug, Ctrl+C, power loss -- can't corrupt the file this way anymore.
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
        # Clean up the temp file on any failure (including the write
        # itself failing) so these don't accumulate; the real target
        # file is untouched either way since we never wrote to it directly.
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _safe_read_json(path, warn_label):
    """
    Load a JSON cache file, tolerating a corrupt/truncated file (e.g. one
    left over from before atomic writes existed, or manual editing gone
    wrong) by warning and treating it as empty rather than crashing the
    whole scrape run. A cache is disposable by design -- worst case is a
    slower run, not a failed one -- so a parse error here should never be
    fatal.
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


DEFAULT_SCRAPED_CACHE_PATH = "scraped_cache.json"
SCRAPED_CACHE_MAX_AGE_DAYS = 1


def load_scraped_cache(path=DEFAULT_SCRAPED_CACHE_PATH):
    """
    Load previously-scraped detail-page fields, keyed by "source:id" (same
    key convention as geocode overrides).

    Only parse_detail()'s output is cached here -- NOT the full row.
    Listing-page fields (status, date_time, street, city_state, etc.)
    always come from a fresh listing-page fetch every run regardless,
    since that's cheap (one request covers all rows on a site) and is
    exactly the data that needs to stay current -- a postponed or
    cancelled auction has to be caught, not papered over by a stale
    cached status. This cache only exists to skip the *expensive* part:
    one HTTP request per individual listing's detail page.

    Each entry: {"scraped_at": "<iso timestamp>", "data": {...}}
    A missing file just means no cache exists yet -- always returns a
    dict, empty or not. A corrupt file is treated the same way (see
    _safe_read_json) rather than crashing the run.
    """
    return _safe_read_json(path, warn_label="scraped-detail cache")


def save_scraped_cache(cache, path=DEFAULT_SCRAPED_CACHE_PATH):
    """
    default=str is a deliberate catch-all here, not laziness: parse_detail()
    is spider-specific and base.py has no way to know in advance whether a
    given site's detail-page fields include a datetime (or Decimal, or any
    other non-JSON-native type). Rather than requiring every spider to
    manually stringify every field it returns, anything json can't
    serialize natively just gets str()'d. For datetime specifically this
    isn't full round-trip fidelity (str() != isoformat()), but this cache
    only exists to skip a re-fetch and get the same detail dict back --
    it's not a datetime storage format, so str() is good enough.

    Written atomically (see _atomic_write_json) so a crash mid-write --
    including a future default=str edge case we haven't hit yet -- can't
    leave behind a half-written, unparseable file.
    """
    _atomic_write_json(cache, path, default=str)


def is_cache_fresh(entry, max_age_days=SCRAPED_CACHE_MAX_AGE_DAYS):
    """True if a cache entry exists and was scraped within max_age_days."""
    if not entry:
        return False
    try:
        scraped_at = datetime.fromisoformat(entry["scraped_at"])
    except (KeyError, ValueError):
        return False
    return (datetime.now() - scraped_at) < timedelta(days=max_age_days)


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

    def scrape(self, scraped_cache_path=DEFAULT_SCRAPED_CACHE_PATH):
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

        scraped_cache = load_scraped_cache(scraped_cache_path)

        total = len(rows)
        for i, row in enumerate(rows, start=1):
            row["auction_dt"], row["timing"] = parse_auction_date(row["date_time"])

            if self.scrape_details and row.get("url"):
                cache_key = f"{self.name}:{row['id']}"
                cached = scraped_cache.get(cache_key)

                if is_cache_fresh(cached):
                    print(f"[{self.name}] ({i}/{total}) CACHED "
                          f"(< {SCRAPED_CACHE_MAX_AGE_DAYS}d old): {row['url']} "
                          f"-- {row.get('street', '')}, {row.get('city_state', '')}")
                    row.update(cached["data"])
                else:
                    print(f"[{self.name}] ({i}/{total}) Scraping: {row['url']} "
                          f"-- {row.get('street', '')}, {row.get('city_state', '')}")
                    try:
                        detail_soup = self.get_soup(row["url"])
                        detail_fields = self.parse_detail(detail_soup, row)
                        row.update(detail_fields)

                        scraped_cache[cache_key] = {
                            "scraped_at": datetime.now().isoformat(),
                            "data": detail_fields,
                        }
                        save_scraped_cache(scraped_cache, scraped_cache_path)
                    except RobotsBlocked as e:
                        print(f"[{self.name}] {e}")
                    except requests.RequestException as e:
                        print(f"[{self.name}] failed on {row['url']}: {e}")
                    time.sleep(self.request_delay)

        return rows