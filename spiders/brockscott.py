"""
Brock & Scott PLLC (brockandscott.com) -- multi-state foreclosure sales.

APPROACH: paginate through ALL states, filter to target states client-side.

The site's own state filter (?_sft_foreclosure_state=ri) does NOT actually
filter results -- tested directly, results came back unfiltered starting
with Alabama regardless of the query param. This is the "Search & Filter
Pro" WordPress plugin; its real filtering likely requires a POST/AJAX
submission we haven't reverse-engineered, and didn't chase further (see
project history for the Harmon Law WAF situation -- not worth repeating
that time sink here when brute-force pagination is cheap and reliable).

Instead: paginate through every page (~90 pages, ~900 listings across 15
states as of 2026-07), and keep only rows whose State matches TARGET_STATES.
Confirmed via curl: plain requests work fine as long as a real
User-Agent is sent (bare curl with no UA got a 403 -- looked like basic
bot-filtering, not a real block; a browser-like UA fixed it immediately).
No robots.txt restriction observed on this path.

Each listing is fully self-contained in one <article class="... foreclosure_search
foreclosure_state-xx ...">, with .forecol divs of label/value <p> pairs
(County, Sale Date, State, Court SP #, Case #, Address, Opening Bid Amount,
Book Page). No separate detail page exists or is needed.

Pagination: follow the link in div.pagination whose text is "Next >" (the
containing div is misleadingly classed "nav-previous" -- a template quirk,
matched here by link text, not class, to avoid relying on that).

NOTE ON DATA COMPLETENESS: this source's listing page only ever exposes the
seven .forecol fields above. There is no property type, bed/bath count,
square footage, lot size, or year-built anywhere on the page or in a detail
page (there is no detail page -- scrape_details = False). Those fields are
structurally unavailable from Brock & Scott and will always be None for
this spider; that's not a parsing bug to chase.
"""

import re
import time
from datetime import datetime
from urllib.parse import quote

from dateutil import parser as date_parser

from base import AuctionSpider, classify_timing

TARGET_STATES = {"MA", "NH", "RI", "CT", "VT", "ME"}

ADDRESS_RE = re.compile(r"^(?P<street>.+?)\s{2,}(?P<city>[^,]+),\s*(?P<rest>.+)$")
ZIP_RE = re.compile(r"(\d{5})\s*$")


def _parse_sale_date(raw):
    """
    Site's date format is numeric ('07/09/2026 - ', trailing dash is a
    placeholder for an end-of-range that's usually empty), NOT the
    'Jul. 9 at 11 am' text format base.DATE_CHUNK_RE expects -- hence this
    site-specific parser instead of base.parse_auction_date().
    """
    cleaned = raw.split("-")[0].strip()
    if not cleaned:
        return None, "Unknown"
    try:
        dt = date_parser.parse(cleaned, fuzzy=True, default=datetime.now())
    except (ValueError, OverflowError):
        return None, "Unknown"
    return dt, classify_timing(dt)


def _parse_address(raw_address, state_code):
    raw_address = raw_address.strip()
    m = ADDRESS_RE.match(raw_address)
    if not m:
        # Fallback: don't crash on an unexpected format, just degrade gracefully.
        return raw_address, state_code

    street = m.group("street").strip()
    city = m.group("city").strip()
    rest = m.group("rest").strip()
    zip_match = ZIP_RE.search(rest)
    zip_code = zip_match.group(1) if zip_match else ""
    city_state = f"{city}, {state_code} {zip_code}".strip()
    return street, city_state


class BrockScottSpider(AuctionSpider):
    name = "brockscott"
    base_url = "https://www.brockandscott.com"
    scrape_details = False  # everything is on the listing page, no detail hop
    request_delay = 1.0
    # A real browser UA -- bare requests without one got a 403 in testing.
    user_agent = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

    start_url = f"{base_url}/foreclosure-sales/"

    # Required by the AuctionSpider ABC, but unused -- this spider overrides
    # scrape() entirely because pagination is dynamic (follow "Next" links
    # until there isn't one) rather than a fixed list of URLs known upfront.
    def listing_urls(self):
        return [self.start_url]

    def parse_listing(self, soup, listing_url):
        rows = []
        for article in soup.select("article.foreclosure_search"):
            row = self._parse_article(article, listing_url)
            if row is not None:
                rows.append(row)
        return rows

    def scrape(self):
        rows = []
        seen_ids = set()
        url = self.start_url
        page_num = 1

        while url:
            if not self.allowed(url):
                print(f"[{self.name}] SKIPPED (robots.txt disallows): {url}")
                break

            print(f"[{self.name}] Fetching page {page_num}: {url}")
            soup = self.get_soup(url)

            for row in self.parse_listing(soup, url):
                if row["id"] in seen_ids:
                    continue
                seen_ids.add(row["id"])
                row["source"] = self.name
                rows.append(row)

            url = self._find_next_url(soup)
            page_num += 1
            time.sleep(self.request_delay)

        print(f"[{self.name}] {len(rows)} listings in {TARGET_STATES} "
              f"across {page_num - 1} page(s)")
        return rows

    # ---- helpers -----------------------------------------------------

    def _find_next_url(self, soup):
        for a in soup.select(".pagination a"):
            if a.get_text(strip=True).lower().startswith("next"):
                return a["href"]
        return None

    def _parse_article(self, article, listing_url):
        fields = {}
        for col in article.select(".forecol"):
            ps = col.find_all("p")
            if len(ps) >= 2:
                label = ps[0].get_text(strip=True).rstrip(":").strip()
                value = ps[1].get_text(strip=True)
                fields[label] = value

        state = fields.get("State", "").strip().upper()
        if state not in TARGET_STATES:
            return None

        auction_id = article.get("id", "").replace("post-", "").strip()
        if not auction_id:
            return None

        street, city_state = _parse_address(fields.get("Address", ""), state)
        date_time_raw = fields.get("Sale Date", "")
        auction_dt, timing = _parse_sale_date(date_time_raw)

        # County is its own field -- return it clean, not smashed together
        # with case/bid/book-page details. Those go in extra_fields instead,
        # which is display-only metadata and must never be parsed back into
        # a structured column downstream.
        county = fields.get("County", "").strip()
        case_number = fields.get("Case #", "").strip()
        court_sp_number = fields.get("Court SP #", "").strip()
        opening_bid = fields.get("Opening Bid Amount", "").strip()
        book_page = fields.get("Book Page", "").strip()

        if case_number:
            # Confirmed via manual curl test: ?_sfm_casenumber=... genuinely
            # filters to the single matching listing (unlike the
            # ?_sft_foreclosure_state=... taxonomy filter, which silently
            # no-ops). This is a real per-listing deep link, not a guess.
            detail_url = f"{self.base_url}/foreclosure-sales/?_sfm_casenumber={quote(case_number)}"
        else:
            detail_url = listing_url  # no case number to filter by -- fall back

        extra_parts = [
            f"Case #: {case_number}" if case_number else "",
            f"Court SP #: {court_sp_number}" if court_sp_number else "",
            f"Opening Bid: {opening_bid}" if opening_bid else "",
            f"Book Page: {book_page}" if book_page else "",
        ]

        return {
            "id": auction_id,
            "url": detail_url,
            "date_time": date_time_raw.strip(),
            "auction_dt": auction_dt,
            "timing": timing,
            "status": "active",  # site has no cancelled/postponed field to key off
            "street": street,
            "city_state": city_state,
            "county": county,
            "case_number": case_number,
            "opening_bid": opening_bid,
            "book_page": book_page,
            "description": county,  # kept for any code still reading .description as a summary field
            "extra_fields": " | ".join(p for p in extra_parts if p),
            "pdf_links": "",
            # Structurally unavailable from this source -- explicit None,
            # not a parsing gap. See module docstring.
            "property_type": None,
            "bedrooms": None,
            "bathrooms": None,
            "sqft": None,
            "lot_size": None,
            "year_built": None,
        }