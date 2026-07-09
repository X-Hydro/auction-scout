"""
Harmon Law Offices (harmonlawoffices.com) -- MA/NH/RI foreclosure auctions.

APPROACH: ID-range enumeration, not their AJAX listing endpoint.

We tried Components/Auction/Assets/AJAX/ListAuctions.php first (the real
DataTables source powering /view-all-auctions) but it's behind an active
WAF (403 with generic block-page headers, even with the right
X-Requested-With/Accept/Referer headers and a session cookie). We did not
attempt to reverse-engineer past that -- that's a live security control,
different in kind from a robots.txt policy.

Individual /auction/{id} pages are different: no WAF block, and each page
already contains everything we need (address, date/status, deposit,
registry/book/page) in one fetch -- no separate listing+detail split
needed. We have direct permission from Harmon Law to test scraping these.

respect_robots = False below is used SPECIFICALLY because of that direct
permission -- robots.txt disallows /auction/*, and normally the base
framework fails closed on that. This is the one intentional, documented
exception in the whole framework. Don't copy this pattern to a new site
without the same kind of explicit go-ahead.

ID range: probe to find real bounds, then set HARMON_ID_START/END below.
Unknown IDs within the range (deleted/never-existed) are detected by the
absence of the '.blockAuction h1' element and silently skipped -- if that
assumption is wrong (we've only confirmed it distinguishes a CANCELLED
auction from a normal one, not yet a genuinely nonexistent ID), tell me
what a real invalid-id page looks like and I'll fix the detection.

DATE/STATUS PARSING: the "when is this auction" info on a detail page
comes in one of three distinct shapes, all confirmed against real pages:

  1. Active/scheduled:  "Begins: 07/15/2026 at 10:00 AM"
  2. Postponed:         "Original Date: 06/29/2026 at 12:00 AM"
                         "Postponed To: 08/31/2026 at 10:00 AM"
                         (TWO separate lines -- the live/current date is
                         the "Postponed To" one, NOT "Original Date".)
  3. Completed/closed:  a plain status phrase with no date at all, e.g.
                         "sold back to mortgagee" or "3rd party purchase"

This used to be detected with base.py's DATE_CHUNK_RE, which matches
TEXT-month dates ("Jul. 9 at 11 am") -- but Harmon's dates are numeric
("07/15/2026 at 10:00 AM"), so that regex never matched, and every
auction on this site was falling into the "it's a status word" branch
regardless of whether it actually had a date. NUMERIC_DATE_RE below
matches Harmon's actual format instead, and _extract_labeled_date() finds
the right line by its label rather than assuming there's exactly one
timedescription element to read.
"""

import re
import time

from base import AuctionSpider

HARMON_ID_START = 1901
HARMON_ID_END = 2000  # known-good starting point, NOT a hard ceiling -- see
# _discover_upper_bound(). New auctions get added over
# time; a fixed end silently stops picking them up with
# no error at all.

# How many consecutive "no auction data" ids in a row before concluding
# we've found the real upper bound. >1 because ids can be individually
# missing/deleted WITHIN the valid range too -- one miss isn't proof we've
# gone past the end.
PROBE_MISS_THRESHOLD = 5
# Safety cap so a bug (e.g. every probed page looking empty) can't spin
# indefinitely making requests.
PROBE_MAX_EXTRA = 50

# Harmon's date format, e.g. "07/15/2026 at 10:00 AM" -- NOT the text-month
# format base.DATE_CHUNK_RE expects, hence the separate regex. See module
# docstring for why this matters.
NUMERIC_DATE_RE = re.compile(
    r"\d{1,2}/\d{1,2}/\d{4}\s+at\s+\d{1,2}:\d{2}\s*[AP]M",
    re.IGNORECASE,
)


def _extract_labeled_date(text, label):
    """
    Find '<label>: <numeric date>' anywhere in text (case-insensitive) and
    return just the date substring, or None if that label isn't present.

    Searching the whole block's text by label -- rather than assuming
    "Begins"/"Original Date"/"Postponed To" each live in one predictable
    .timedescription-style element -- is what lets this handle the
    postponed case's two separate date lines without depending on exact
    markup we can't fully verify (this site's postponed pages appear to
    render "Original Date" and "Postponed To" as two distinct lines, not
    one element with both).
    """
    pattern = re.compile(
        re.escape(label) + r"\s*:\s*(" + NUMERIC_DATE_RE.pattern + r")",
        re.IGNORECASE,
        )
    m = pattern.search(text)
    return m.group(1) if m else None


def _parse_registry(registry_text):
    """
    The .registry element's text is "Registry: <county> Book / Page:
    <book/page>" (both lines collapsed onto one line by get_text(" ")) --
    split it into (county, book_page). Tolerates the "Registry:" label
    prefix being absent (in case the selector already excludes it) and
    tolerates "Book / Page:" being absent entirely (some listings may not
    have it) -- in either case, falls back to treating whatever's left as
    the county rather than losing the value.
    """
    if not registry_text:
        return "", ""
    text = re.sub(r"^\s*Registry:\s*", "", registry_text, flags=re.IGNORECASE)
    m = re.search(r"\s*Book\s*/\s*Page:\s*(.*)$", text, re.IGNORECASE)
    if m:
        return text[:m.start()].strip(), m.group(1).strip()
    return text.strip(), ""


class HarmonSpider(AuctionSpider):
    name = "harmon"
    base_url = "https://www.harmonlawoffices.com"
    scrape_details = False  # each "listing" IS the detail page, no second fetch needed
    respect_robots = False  # see module docstring -- direct permission granted

    def listing_urls(self):
        end = self._discover_upper_bound()
        return [
            f"{self.base_url}/auction/{i}"
            for i in range(HARMON_ID_START, end + 1)
        ]

    def _discover_upper_bound(self):
        """
        Probe forward one id at a time past HARMON_ID_END, using the same
        '.blockAuction h1 present?' signal parse_listing() already uses to
        detect a nonexistent id, until PROBE_MISS_THRESHOLD consecutive
        misses confirm we've gone past the real end. Returns the last id
        that had real auction data (HARMON_ID_END itself if nothing new
        was found). Capped at PROBE_MAX_EXTRA requests.

        This runs on every scrape -- the fixed cost is up to
        PROBE_MISS_THRESHOLD extra requests when nothing new exists yet,
        which is the price of not silently missing new auctions.
        """
        end = HARMON_ID_END
        misses_in_a_row = 0
        probed = 0
        candidate = HARMON_ID_END + 1

        while misses_in_a_row < PROBE_MISS_THRESHOLD and probed < PROBE_MAX_EXTRA:
            url = f"{self.base_url}/auction/{candidate}"
            try:
                soup = self.get_soup(url)
                has_auction = bool(soup.select_one(".blockAuction h1"))
            except Exception as e:
                print(f"[{self.name}] probe failed at {url}: "
                      f"{type(e).__name__}: {e} -- treating as miss")
                has_auction = False

            if has_auction:
                end = candidate
                misses_in_a_row = 0
            else:
                misses_in_a_row += 1

            probed += 1
            candidate += 1
            time.sleep(self.request_delay)

        if end > HARMON_ID_END:
            print(f"[{self.name}] found {end - HARMON_ID_END} new auction id(s) "
                  f"beyond hardcoded HARMON_ID_END ({HARMON_ID_END}) -- "
                  f"extending range to {end}. Consider updating HARMON_ID_END "
                  f"in the source to save future probe requests.")

        return end

    def parse_listing(self, soup, listing_url):
        block = soup.select_one(".blockAuction")
        h1 = block.select_one("h1") if block else None
        if not h1:
            print(f"[{self.name}] no auction data at {listing_url} -- skipping")
            return []

        auction_id = self._extract_id(soup, listing_url)
        if not auction_id:
            print(f"[{self.name}] couldn't determine id at {listing_url} -- skipping")
            return []

        street, city_state = self._parse_location(block)
        auction_type = self._text(block.select_one("h2"))
        deposit = self._parse_deposit(block)
        timedescription = self._text(block.select_one(".timedescription"))
        registry_raw = self._text(block.select_one(".registry"))
        county, book_page = _parse_registry(registry_raw)

        # Search the whole block's text for each labeled date, rather than
        # relying on .timedescription alone -- see module docstring for
        # why (postponed auctions have two separate date lines, and
        # Harmon's numeric date format needs its own regex, not
        # base.DATE_CHUNK_RE's text-month pattern).
        block_text = block.get_text(" ", strip=True)
        postponed_to = _extract_labeled_date(block_text, "Postponed To")
        begins = _extract_labeled_date(block_text, "Begins")
        original_date = _extract_labeled_date(block_text, "Original Date")

        if postponed_to:
            date_time = postponed_to
            status = "postponed"
        elif begins:
            date_time = begins
            status = "active"
        elif original_date:
            # Postponed with no new date announced yet -- keep the status,
            # but there's no current date to report.
            date_time = ""
            status = "postponed"
        else:
            # No date pattern matched at all -- timedescription must hold
            # a genuine completed/closed status phrase (e.g. "sold back to
            # mortgagee", "3rd party purchase").
            date_time = ""
            status = timedescription.strip().lower() or "unknown"

        extra_parts = [
            f"Deposit: {deposit}" if deposit else "",
            f"Book/Page: {book_page}" if book_page else "",
            # Keep the original pre-postponement date as a record even
            # though "Postponed To" is what drives date_time/status above.
            f"Original Date: {original_date}" if (original_date and postponed_to) else "",
        ]

        description_parts = [p for p in [auction_type, registry_raw] if p]

        return [{
            "id": str(auction_id),
            "url": listing_url,
            "date_time": date_time,
            "status": status,
            "street": street,
            "city_state": city_state,
            "county": county,
            "description": " | ".join(description_parts),
            "extra_fields": " | ".join(p for p in extra_parts if p),
            "pdf_links": "",
        }]

    # ---- Harmon-specific parsing helpers ---------------------------

    @staticmethod
    def _text(el):
        return el.get_text(" ", strip=True) if el else ""

    def _extract_id(self, soup, listing_url):
        hidden = soup.select_one('input[name="AuctionID"]')
        if hidden and hidden.get("value"):
            return hidden["value"]
        m = re.search(r"/auction/(\d+)", listing_url)
        return m.group(1) if m else None

    def _parse_location(self, block):
        loc = block.select_one(".location")
        if not loc:
            return "", ""
        span = loc.find("span")
        city_state = self._text(span)
        full = self._text(loc)
        street = full.replace("Location:", "").replace(city_state, "").strip()
        return street, city_state

    def _parse_deposit(self, block):
        for strong in block.find_all("strong"):
            text = strong.get_text(strip=True)
            if text.lower().startswith("deposit"):
                return text.split(":", 1)[-1].strip()
        return ""