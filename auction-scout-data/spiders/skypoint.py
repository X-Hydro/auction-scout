"""
Skypoint Auctions, LLC (skypointauctions.com) -- MA/NH/RI/VT foreclosure
and condo auctions.

STATUS: DRAFT, NOT YET VERIFIED AGAINST RAW HTML. I only have this site's
content as fetched through a markdown-converting web tool, not raw DOM --
so I can't confirm real element/class names the way harmon.py's
'.blockAuction h1' or jjmanning.py's 'article[class*="type-auction"]' are
confirmed. Before running this for real: view-source the homepage and
check the two assumptions flagged below (ONE_LISTING_PER_TABLE and the
id/deposit signature filter). Everything else here is regex over
get_text(), which is markup-agnostic and should survive minor DOM
differences.

SITE STRUCTURE (confirmed from fetched content):
Single static homepage (Weebly-hosted, "Website by Digital Media Design,
Inc." in the footer) renders ALL listings inline -- a "Current Auctions"
section followed by a "Recent Auctions" section, no AJAX, no pagination,
no WAF observed. Same "no separate listing+detail split needed" shape as
harmon.py, except here it's the LISTING page itself (not a per-auction
detail page) that already has everything: id, address, date/status,
deposit, and PDF link(s). scrape_details = False accordingly.

Two older pages, archives-2024-25.html and archives-previous.html, hold
further back history in the same format -- not included in
listing_urls() below since the tracker only needs current/recent state,
same scope decision as jjmanning's single calendar page. Add them here
if backfill is ever wanted.

ASSUMPTION #1 -- ONE_LISTING_PER_TABLE: the fetched markdown renders each
listing as its own pipe-table (image cell optionally + text cell), which
strongly suggests the underlying HTML is a sequence of small <table>
elements in the main content column, one per listing, separated by <hr>.
parse_listing() below finds every <table> on the page and keeps only the
ones whose text matches the listing signature (see ASSUMPTION #2) --
rather than depending on a specific class name I haven't verified.

ASSUMPTION #2 -- SIGNATURE FILTER: a real listing table is identified by
its TEXT containing both an id pattern (#2653 / SP #2513 / #2649A) and
the phrase "Required Deposit" -- not by class name. This is deliberately
markup-agnostic (same philosophy as harmon.py's block-text label search)
so it keeps working even if Weebly's editor changes wrapper divs/classes,
at the cost of being slightly slower (every <table> on the page gets its
text checked).

STATUS KEYWORDS (confirmed present, inconsistent spelling in the wild):
  "SOLD:"                    -> closed, sale completed
  "CANCELED:" / "CANCELLED:" -> closed, both spellings seen on the same
                                 site (e.g. "CANCELED" on #2650 vs
                                 "CANCELLED" on #2642) -- STATUS_RE below
                                 matches both.
  "NEW DATE:" / "New Date:"  -> rescheduled, still upcoming; the site
                                 also prints "Previously scheduled for
                                 <old date>" right after -- kept as
                                 extra_fields, not used as the live date.
  (none of the above)        -> plain "<Weekday>, <Month> <Day> @ <time>"
                                 with no label at all means still
                                 scheduled/active (e.g. #2643's first
                                 appearance in Recent Auctions).

DATES HAVE NO YEAR ("Friday, August 7th @ 12:00 PM"), same problem as
patriot.py -- reusing that file's fuzzy-parse-plus-rollover approach
rather than reinventing it, since the failure mode (year-boundary
misparse) and fix are identical.

DEDUP: the id space is shared between "Current Auctions" and "Recent
Auctions" (e.g. an id could plausibly show up as an upcoming NEW DATE row
and, after the auction runs, as a SOLD row lower on the page during some
future scrape) -- STATUS_PRIORITY dedup here, same priority-merge pattern
as patriot.py/towne.py, keyed on the site's own "#NNNN"-style id with any
"SP" prefix stripped and any letter suffix (2649A/2649B/2649C/2649D --
confirmed these are distinct sub-lots of one property, not typos) kept
intact so they don't collide with each other.

OLD SOLD LISTINGS ARE DROPPED HERE, NOT DOWNSTREAM -- deliberate
exception to the "spider reports everything, export_json.py decides
what's live" split used everywhere else in this codebase (see
export_json.py's EXCLUDED_STATUSES, which already keeps ALL sold
listings off the live map regardless of age -- that part is unchanged).
The reason this site specifically needs its own cutoff: unlike the other
five sources, Skypoint's homepage keeps SOLD entries inline indefinitely
(the fetched sample already had 12+ month-old sold listings on the same
page as this week's active ones) -- there's no pagination or archive-page
split pushing them out of scope the way there is elsewhere. Since this
spider runs weekly, a sold auction has already had at least one full run
to be captured/audited with an explicit SOLD status; re-ingesting it
forever afterwards is pure noise, not new information -- so a status=SOLD
row is dropped here once its date is more than SOLD_STALE_AFTER_DAYS in
the past. A sold row with a date we couldn't parse is kept rather than
guessed at -- see the skip check in parse_listing().
"""

import re
from datetime import datetime, timedelta
from urllib.parse import urljoin

from dateutil import parser as date_parser

from base import AuctionSpider, clean_url

STATUS_PRIORITY = {"active": 2, "sold": 1, "canceled": 1}

# Spider runs weekly -- a SOLD listing has already had at least one run to
# be captured with an explicit status, so there's no new information in
# continuing to report it once it's this stale. See module docstring
# ("OLD SOLD LISTINGS ARE DROPPED HERE") for why this lives here instead
# of in export_json.py alongside the other status-based exclusions.
SOLD_STALE_AFTER_DAYS = 14

ID_RE = re.compile(r"(?:SP\s*)?#\s*(\d+[A-Za-z]?)")
DEPOSIT_RE = re.compile(r"Required Deposit\s*:?\s*\$?([\d,]+)")
STATUS_RE = re.compile(
    r"(SOLD|CANCELED|CANCELLED|NEW DATE)\s*:?\s*(.*?)(?=Required Deposit|Previously scheduled|$)",
    re.IGNORECASE,
)
PREV_SCHEDULED_RE = re.compile(r"Previously scheduled for ([^R]+?)(?=Required Deposit|$)", re.IGNORECASE)
# City, ST pair, e.g. "Pittsfield, MA -" -- captures just the city/state;
# the street itself is found separately by STREET_RE below, anchored to
# where this match ends. The separator after the state is optional and
# accepts a hyphen, en dash, or em dash, or nothing at all -- confirmed
# from real listings that the site is NOT consistent here: most use a
# plain "-", but #2440 ("Wareham, MA \u2013 3 Bedroom...") uses an en dash,
# and #2538 ("Springfield, MA 32-34 Marsden St...") uses no separator at
# all, just a space. Both previously caused a total parse failure (empty
# street AND city_state) because the old pattern required a literal "-".
CITY_STATE_RE = re.compile(
    r"([A-Za-z .'\u2019-]+,\s*(?:MA|NH|RI|VT|ME|CT))\s*(?:[-\u2013\u2014]\s*)?"
)

# A real street token: house number, a handful of words, then a genuine
# street-type suffix -- NOT "capture until the next field starts". The
# earlier version tried to find where the address text ENDS (a double
# space, then a status keyword/date/deposit boundary), and both were
# wrong for the same underlying reason: BeautifulSoup's
# get_text(" ", strip=True) joins every text node with exactly ONE space
# regardless of the source markup, so there's no reliable whitespace or
# "next field" signal separating the street from a trailing property
# descriptor that sits on the same line with no keyword of its own (e.g.
# "512 Lincoln St - 3 Bedroom Ranch" -- confirmed from real geocoding
# failures where "Two Bedroom Cape", "3,300 SF Three Family", etc. all
# ended up glued onto the Street field).
#
# Matching the street's own shape instead sidesteps that entirely: a
# street address has a recognizable end (its suffix word), so there's no
# need to know where the NEXT thing starts. Searching forward from where
# CITY_STATE_RE ends (rather than requiring an immediate match) also
# turns out to correctly handle the one listing on this site where the
# order is reversed -- "Wareham, MA \u2013 3 Bedroom Colonial Home 21
# Nicholas Drive" -- since the regex just keeps scanning until it finds
# a number+suffix shape, wherever it actually sits.
#
# The unit is captured in its OWN group (group 2), not merged into the
# street (group 1): Census/Nominatim geocode noticeably worse with a
# unit number appended (confirmed -- e.g. "380 Harrison Ave Unit #1004"
# failed both geocoders, but "380 Harrison Ave" alone should not). This
# row's "unit" field is used by run-scout.py to fold the unit back into
# the displayed Name ("84 Winthrop Rd Unit #1, Brookline, MA") while
# geocode.py's strip_unit_for_geocoding() keeps it out of the string
# actually sent to the geocoder -- no dedicated DB column needed; it
# rides along in address_raw via that folded Name string. Matched only when clearly
# marked as a unit (", 6B" -- comma right after the suffix -- or "Unit
# #1"/"Unit 18"), specifically so it does NOT also grab the next bare
# number it sees (e.g. "104 Emerald St 17,399 SF..." must NOT capture
# "17" as a unit -- there's no comma between "St" and "17,399", so the
# comma-gated branch correctly refuses to match there).
_STREET_SUFFIX = (
    r"(?:Road|Rd|Street|St|Avenue|Ave|Drive|Dr|Lane|Ln|Way|Court|Ct|"
    r"Boulevard|Blvd|Place|Pl|Circle|Cir|Terrace|Ter|Highway|Hwy|"
    r"Parkway|Pkwy|Trail|Trl|Square|Sq|Path|Row|Extension|Ext)\.?"
)
STREET_RE = re.compile(
    r"(\d+[\w-]*(?:\s+[A-Za-z][\w'.-]*){0,5}?\s+" + _STREET_SUFFIX + r")"
                                                                     r"((?:,\s*(?:Unit\s*#?\s*\w+|#\s*\w+|\d+[A-Za-z]))"  # ", 6B" / ", #103"
                                                                     r"|(?:\s+Unit\s*#?\s*\w+))?",                          # "Unit #1" (no comma)
    re.IGNORECASE,
    )
PLAIN_DATE_RE = re.compile(
    r"\b((?:Mon|Tues?|Wed(?:nes)?|Thu(?:rs)?|Fri|Sat(?:ur)?|Sun)[a-z]*,\s*[A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?\s*@\s*\d{1,2}:\d{2}\s*[AP]M)",
    re.IGNORECASE,
)


def _parse_no_year_date(text, reference=None):
    """Same rollover logic as patriot.py's _parse_no_year_date -- this
    site's dates never include a year either, so the fix for the same
    problem (a January auction scraped in December misparsing into the
    wrong year) is identical. See patriot.py's docstring for the reasoning."""
    reference = reference or datetime.now()
    text = (text or "").strip()
    if not text:
        return None
    try:
        dt = date_parser.parse(text, fuzzy=True, default=reference)
    except (ValueError, OverflowError):
        return None
    if dt < reference - timedelta(days=30):
        dt = dt.replace(year=dt.year + 1)
    return dt


def _looks_like_listing(text):
    """Signature filter -- see ASSUMPTION #2 in the module docstring."""
    return bool(ID_RE.search(text)) and "required deposit" in text.lower()


class SkypointSpider(AuctionSpider):
    name = "skypoint"
    base_url = "https://www.skypointauctions.com"
    scrape_details = False  # everything needed is already on the homepage table

    def listing_urls(self):
        return [f"{self.base_url}/"]

    def parse_listing(self, soup, listing_url):
        rows_by_id = {}

        # ASSUMPTION #1: one <table> per listing. VERIFY against raw HTML
        # before trusting this in production -- if the real markup groups
        # listings some other way (e.g. one big table, or <div> blocks
        # instead of <table>), swap this loop's source but keep
        # _looks_like_listing() as the filter either way.
        for table in soup.find_all("table"):
            text = table.get_text(" ", strip=True)
            if not _looks_like_listing(text):
                continue

            id_match = ID_RE.search(text)
            auction_id = id_match.group(1) if id_match else None
            if not auction_id:
                continue

            loc_match = CITY_STATE_RE.search(text)
            city_state = ""
            street = ""
            unit = ""
            if loc_match:
                city_state = loc_match.group(1).strip()
                # Search for the street token starting where the "City,
                # ST" match ends (this is a forward SEARCH, not an
                # anchored match right at that position -- deliberately,
                # since at least one real listing puts descriptive text
                # between the city/state and the actual street; see
                # STREET_RE's docstring comment for the confirmed case).
                street_match = STREET_RE.search(text, loc_match.end())
                if street_match:
                    street = street_match.group(1).strip()
                    unit_raw = (street_match.group(2) or "").strip(" ,")
                    if unit_raw:
                        # Normalize to "Unit <value>" -- ", 6B" and
                        # "Unit 18" both become "Unit 6B"/"Unit 18";
                        # "Unit #1004" is left as-is (already has "Unit").
                        unit = unit_raw if unit_raw.lower().startswith("unit") \
                            else f"Unit {unit_raw}"

            deposit_match = DEPOSIT_RE.search(text)
            deposit = deposit_match.group(1) if deposit_match else ""

            status_match = STATUS_RE.search(text)
            if status_match:
                keyword = status_match.group(1).upper()
                date_text_raw = status_match.group(2).strip()
                if keyword == "SOLD":
                    status = "sold"
                elif keyword in ("CANCELED", "CANCELLED"):
                    status = "canceled"
                else:  # NEW DATE
                    status = "active"  # rescheduled, still upcoming
                date_match = PLAIN_DATE_RE.search(date_text_raw) or PLAIN_DATE_RE.search(text)
            else:
                # No status keyword at all -- plain scheduled listing.
                status = "active"
                date_match = PLAIN_DATE_RE.search(text)

            date_time_text = date_match.group(1) if date_match else ""
            auction_dt = _parse_no_year_date(date_time_text)

            # Drop stale SOLD rows -- see SOLD_STALE_AFTER_DAYS and the
            # module docstring for why. A sold row whose date we couldn't
            # parse (auction_dt is None) is deliberately KEPT rather than
            # guessed at -- we have no reliable way to know it's actually
            # stale, and silently dropping an un-datable row is worse than
            # one extra week of a listing lingering in the output.
            if (
                    status == "sold"
                    and auction_dt is not None
                    and auction_dt < datetime.now() - timedelta(days=SOLD_STALE_AFTER_DAYS)
            ):
                continue

            prev_match = PREV_SCHEDULED_RE.search(text)
            previously_scheduled = prev_match.group(1).strip() if prev_match else ""

            pdf_links = [
                clean_url(urljoin(self.base_url, a["href"]))
                for a in table.find_all("a", href=True)
                if a["href"].lower().endswith(".pdf")
            ]

            extra_parts = [
                f"Deposit: ${deposit}" if deposit else "",
                f"Previously scheduled for {previously_scheduled}" if previously_scheduled else "",
            ]

            row = {
                "id": auction_id,
                "url": listing_url,
                "date_time": date_time_text,
                "auction_dt": auction_dt,
                "status": status,
                "street": street,
                "city_state": city_state,
                "unit": unit,
                "description": "",  # site rarely gives a separate description beyond address
                "extra_fields": " | ".join(p for p in extra_parts if p),
                "pdf_links": "; ".join(pdf_links),
            }

            existing = rows_by_id.get(auction_id)
            if existing is None:
                rows_by_id[auction_id] = row
            elif STATUS_PRIORITY.get(status, 0) > STATUS_PRIORITY.get(existing["status"], 0):
                rows_by_id[auction_id] = row

        return list(rows_by_id.values())