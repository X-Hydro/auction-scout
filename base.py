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
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

DATE_CHUNK_RE = re.compile(
    r"[A-Za-z]{3,9}\.?\s+\d{1,2}(?:,\s*\d{4})?\s+at\s+\d{1,2}(?::\d{2})?\s*[ap]m",
    re.IGNORECASE,
)


def parse_auction_date(text, upcoming_window_days=7):
    """
    Parse a scraped date string into a datetime, and classify it as
    'This Week' / 'Later' / 'Past' / 'Unknown'.

    Handles postponed-auction strings that contain TWO dates
    ('Thu. Jul. 9 at 11 am Mon. Aug. 10, 2026 at 1 pm') by taking the
    LAST date chunk, which is always the current/rescheduled one.
    """
    matches = DATE_CHUNK_RE.findall(text)
    candidate = matches[-1] if matches else text

    try:
        dt = date_parser.parse(candidate, fuzzy=True, default=datetime.now())
    except (ValueError, OverflowError):
        return None, "Unknown"

    delta_days = (dt - datetime.now()).total_seconds() / 86400
    if delta_days < 0:
        timing = "Past"
    elif delta_days <= upcoming_window_days:
        timing = "This Week"
    else:
        timing = "Later"
    return dt, timing


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

    def __init__(self):
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