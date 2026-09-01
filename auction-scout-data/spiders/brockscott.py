"""
Brock & Scott PLLC (brockandscott.com) -- multi-state foreclosure sales.

Site redesigned 2026-09 (new "brock-and-scott-2026" WordPress theme). This
is a rewrite against the new markup -- the entire "brute-force paginate
every state, filter client-side" approach from the old site is gone, along
with everything it was built around. Notes below reflect the new site only.

APPROACH: fetch each of our 6 TARGET_STATES directly via its own
?_sft_foreclosure_state=<code> filter, one state at a time, paginating
within just that state.

CONFIRMED WORKING (unlike the old site): ?_sft_foreclosure_state=nh
genuinely filters to NH-only results now -- verified directly against live
HTML, page literally reports "Showing 20 of 60 foreclosure sales in NH".
The old module docstring's entire rationale for brute-forcing ("the site's
own state filter does NOT actually filter results") no longer holds; keep
that in mind if this comment and the code ever drift, since it means this
spider now only ever requests pages for MA/NH/RI/CT/VT/ME, never the other
~9 states the firm also covers.

Each state's results page reports its own expected total up front
(#results-count, e.g. "Showing 20 of 60 foreclosure sales in NH") --
scrape() cross-checks the parsed row count against this per state and
warns on any mismatch, the same self-verifying pattern used for CT
Judicial's town index (see spiders/ct_judicial.py) and now built into
run-scout.py's own EXPECTED_COUNTS range check at the aggregate level.

MARKUP: listings are now a real HTML table (table.bs-table > tbody > tr),
NOT the old <article class="foreclosure_search"> / .forecol label-value
div structure -- that's gone entirely. Columns, in fixed order: County,
Sale Date, State, Court SP #, Case #, Address, Opening Bid Amt., Book Page
-- parsed positionally by <td> index, not by any label text (there isn't
one anymore).

NO MORE POST ID: the old site exposed a WordPress post id on the
<article> (id="post-XXXX") that this spider used as its unique row id.
That's gone -- there's no id attribute anywhere on a <tr>. Case Number is
now the row id instead; every real listing has one, and it was already
confirmed elsewhere (case-number deep link, see detail_url below) to be a
genuinely unique per-listing identifier, not a guess.

SALE TIME IS NEW: the old site's Sale Date field was date-only, no time
("07/09/2026 -", trailing dash a placeholder for an always-empty range
end). The new markup exposes both a machine-readable ISO date
(<time datetime="2026-08-04">) AND a real sale time
(<span class="sale-time">&middot; 10:00 AM</span>) that never existed
before -- see _parse_sale_cell().

Confirmed via curl (still true post-redesign): plain requests work fine
as long as a real User-Agent is sent (bare curl with no UA got a 403).
No robots.txt restriction observed on this path.

NOTE ON DATA COMPLETENESS: this source's listing table only ever exposes
the eight columns above. There is no property type, bed/bath count,
square footage, lot size, or year-built anywhere on the page or in a
detail page (there is no separate detail page -- scrape_details = False).
Those fields are structurally unavailable from Brock & Scott and will
always be None for this spider; that's not a parsing gap to chase.
"""

import re
import time
import random
from datetime import datetime
from urllib.parse import quote
from requests.exceptions import HTTPError

from dateutil import parser as date_parser

from base import AuctionSpider, classify_timing

TARGET_STATES = {"MA", "NH", "RI", "CT", "VT", "ME"}

ADDRESS_RE = re.compile(r"^(?P<street>.+?)\s{2,}(?P<city>[^,]+),\s*(?P<rest>.+)$")
ZIP_RE = re.compile(r"(\d{5})\s*$")

# New-site addition: real sale time, e.g. "10:00 AM" inside
# <span class="sale-time">&middot; 10:00 AM</span>. Extracted by regex
# rather than string-stripping the leading middot, since that's more
# robust to any whitespace/entity variation between listings.
TIME_RE = re.compile(r"(\d{1,2}:\d{2}\s*[AP]M)", re.IGNORECASE)

# Matches the new site's own per-state result count, e.g.
# "Showing 20 of 60 foreclosure sales in NH" -- used as a source-verified
# cross-check against what actually got parsed, same pattern as CT
# Judicial's town index.
RESULTS_COUNT_RE = re.compile(r"of\s+(\d+)\s+foreclosure sales", re.IGNORECASE)


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
    scrape_details = False  # everything is on the listing table, no detail hop
    request_delay = 2.0
    max_retries = 4
    backoff_base = 5             # seconds
    # A real browser UA -- bare requests without one got a 403 in testing.
    user_agent = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

    start_url = f"{base_url}/foreclosure-sales/"

    # Required by the AuctionSpider ABC, but unused -- this spider overrides
    # scrape() entirely: it fetches one URL per TARGET_STATES entry (each
    # with its own dynamic, unknown-in-advance page count), not a fixed
    # list of URLs known upfront.
    def listing_urls(self):
        return [self.start_url]

    def parse_listing(self, soup, listing_url):
        rows = []
        table = soup.select_one("table.bs-table")
        if not table:
            return rows
        tbody = table.find("tbody")
        if not tbody:
            return rows
        for tr in tbody.find_all("tr"):
            row = self._parse_row(tr, listing_url)
            if row is not None:
                rows.append(row)
        return rows

    def scrape(self):
        rows = []
        seen_ids = set()

        for state in sorted(TARGET_STATES):
            state_rows, expected_total = self._scrape_state(state)
            for row in state_rows:
                if row["id"] in seen_ids:
                    continue
                seen_ids.add(row["id"])
                row["source"] = self.name
                rows.append(row)

            if expected_total is not None and len(state_rows) != expected_total:
                print(f"[{self.name}] WARNING: {state}: site's own "
                      f"results-count said {expected_total}, but parsed "
                      f"{len(state_rows)} -- markup may have changed for "
                      f"this state, or a row failed to parse.")

        print(f"[{self.name}] {len(rows)} listings across "
              f"{len(TARGET_STATES)} target state(s)")
        return rows

    def _scrape_state(self, state):
        """Fetch every page for one state's filtered results. Returns
        (rows, expected_total) -- expected_total is the site's own
        reported count for this state (from #results-count on the first
        page), or None if that couldn't be read."""
        state_code = state.lower()
        url = f"{self.base_url}/foreclosure-sales/?_sft_foreclosure_state={state_code}"
        rows = []
        expected_total = None
        page_num = 1

        while url:
            if not self.allowed(url):
                print(f"[{self.name}] SKIPPED (robots.txt disallows): {url}")
                break

            print(f"[{self.name}] Fetching {state} page {page_num}: {url}")
            soup = self._get_soup_with_retry(url)
            if soup is None:
                print(f"[{self.name}] Giving up on {url} after {self.max_retries} retries")
                break

            if expected_total is None:
                expected_total = self._parse_expected_total(soup)

            rows.extend(self.parse_listing(soup, url))

            url = self._find_next_url(soup)
            page_num += 1
            time.sleep(self.request_delay + random.uniform(0, 1.5))

        return rows, expected_total

    # ---- helpers -----------------------------------------------------
    def _get_soup_with_retry(self, url):
        for attempt in range(1, self.max_retries + 1):
            try:
                return self.get_soup(url)
            except HTTPError as e:
                resp = e.response
                if resp is not None and resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after else self.backoff_base * (2 ** (attempt - 1))
                    print(f"[{self.name}] 429 on attempt {attempt}/{self.max_retries} "
                          f"-- waiting {wait:.0f}s")
                    time.sleep(wait)
                    continue
                raise
        return None

    def _find_next_url(self, soup):
        for a in soup.select(".pagination a"):
            if a.get_text(strip=True).lower().startswith("next"):
                return a["href"]
        return None

    def _parse_expected_total(self, soup):
        el = soup.select_one("#results-count")
        if not el:
            return None
        m = RESULTS_COUNT_RE.search(el.get_text(" ", strip=True))
        return int(m.group(1)) if m else None

    def _parse_sale_cell(self, date_cell):
        """Read the new site's <time datetime="2026-08-04">08/04/2026
        <span class="sale-time">&middot; 10:00 AM</span></time> structure.
        Real sale TIME is new on this site -- the old markup only ever had
        a date. Returns (auction_dt, timing, display_string)."""
        time_tag = date_cell.find("time")
        if not time_tag:
            raw = date_cell.get_text(strip=True)
            return None, "Unknown", raw

        iso_date = time_tag.get("datetime", "").strip()
        time_span = time_tag.find("span", class_="sale-time")
        time_match = TIME_RE.search(time_span.get_text(" ", strip=True)) if time_span else None
        time_text = time_match.group(1) if time_match else ""

        combined = f"{iso_date} {time_text}".strip()
        try:
            auction_dt = date_parser.parse(combined, fuzzy=True, default=datetime.now())
        except (ValueError, OverflowError):
            auction_dt = None

        if auction_dt is not None:
            timing = classify_timing(auction_dt)
            display = auction_dt.strftime("%m/%d/%Y %I:%M %p")
        else:
            timing = "Unknown"
            display = date_cell.get_text(strip=True)

        return auction_dt, timing, display

    def _parse_row(self, tr, listing_url):
        cells = tr.find_all("td")
        if len(cells) < 8:
            return None  # unexpected row shape -- skip rather than crash

        # Fixed column order confirmed against live markup: County, Sale
        # Date, State, Court SP #, Case #, Address, Opening Bid Amt.,
        # Book Page. No labels anymore -- purely positional.
        county = cells[0].get_text(strip=True)
        state = cells[2].get_text(strip=True).upper()
        court_sp_number = cells[3].get_text(strip=True)
        case_number = cells[4].get_text(strip=True)
        address_raw = cells[5].get_text(strip=True)
        opening_bid = cells[6].get_text(strip=True)
        book_page = cells[7].get_text(strip=True)

        if state not in TARGET_STATES:
            return None  # shouldn't happen given the state-filtered fetch, but cheap insurance
        if not case_number:
            return None  # no reliable unique id without it -- see module docstring

        auction_dt, timing, date_time_display = self._parse_sale_cell(cells[1])
        street, city_state = _parse_address(address_raw, state)

        # Confirmed via manual curl test: ?_sfm_casenumber=... genuinely
        # filters to the single matching listing. This is a real
        # per-listing deep link, not a guess.
        detail_url = f"{self.base_url}/foreclosure-sales/?_sfm_casenumber={quote(case_number)}"

        extra_parts = [
            f"Case #: {case_number}" if case_number else "",
            f"Court SP #: {court_sp_number}" if court_sp_number else "",
            f"Opening Bid: {opening_bid}" if opening_bid else "",
            f"Book Page: {book_page}" if book_page else "",
        ]

        return {
            # Case Number is the row id now -- the old site's WordPress
            # post id (id="post-XXXX" on the <article>) doesn't exist in
            # the new table markup at all. See module docstring.
            "id": case_number,
            "url": detail_url,
            "date_time": date_time_display,
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