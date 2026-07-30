"""
Landmark Auction Co., LLC (landmarkauction.biz) -- MA/NH/RI/VT foreclosures,
secured party sales, and liquidations.

STATUS: DRAFT, NOT YET VERIFIED AGAINST RAW HTML -- same caveat as
skypoint.py. I only have this site's content as fetched through a
markdown-converting web tool, not the raw DOM, so I can't confirm real
element/class names. Before running this for real: view-source the
homepage and check ASSUMPTION #1 below (EVENTLIST selector). Everything
else here is regex over get_text(), which is markup-agnostic and should
survive minor DOM differences -- same philosophy as harmon.py's block-text
label search and skypoint.py's signature filter.

SITE STRUCTURE (confirmed from fetched content):
This is a Squarespace "Events" collection (summary-block event list --
the site is Squarespace-hosted per the CDN image URLs). The homepage IS
the calendar ("/") and renders both an "upcoming" section (chronological
ascending) and a "past/recent" section (reverse chronological) in one
static page load, separated by a "---" divider in the fetched text -- no
pagination observed, no separate listing+detail split needed.
scrape_details = False accordingly -- confirmed by fetching a detail page
directly (see e.g. /auction-calendar/2026/7/30/46-george-st-plainville-ma-02762):
it has zero fields beyond what's already on the calendar page (same
date/status/book-page/map, no deposit or terms text, no PDFs).

ASSUMPTION #1 -- EVENTLIST SELECTOR: each event is presumed to render as
an `article.eventlist-event` (Squarespace 7.1's standard Events block
markup -- more likely to be stable across Squarespace sites generally
than harmon/jjmanning/patriot's site-specific custom markup, but still
unverified for THIS site/template). If that selector matches nothing,
_fallback_containers() reconstructs event blocks a different way: finds
every link to an event detail page (/auction-calendar/YYYY/M/D/slug),
then climbs that link's DOM ancestors (capped at 8 levels) until the
accumulated text contains both an <h1> and a recognizable status/date
signature (a status phrase or "Book ... at Page") -- same
identify-by-content-signature approach as skypoint.py's
ONE_LISTING_PER_TABLE / SIGNATURE FILTER. This fallback is inherently
fuzzier (it can climb too far and swallow a neighboring event's text on
an unfamiliar layout) -- if it's actually being used (watch for the
printed warning), spot-check a few rows by hand before trusting the
output.

IDENTIFYING EACH LISTING: unlike harmon/jjmanning/patriot/sullivan, this
site has no separate numeric auction id anywhere -- the closest thing is
the URL itself, which is date+address-derived
(/auction-calendar/2026/7/30/130-rachael-ter-westfield-ma-01085-2nd-mortgage).
That's used as the id verbatim. Critically, this means a postponement
gets a NEW id rather than a status change on the old one -- confirmed
from the sample: "12 Sunnyside Dr, Durham, NH 03824" still sits at its
original 7/28 URL with status text "Continued to August 28 at 4 PM"
rather than moving to a new URL. So no cross-listing dedup is needed here
(each URL is inherently unique on a given scrape) -- unlike
patriot.py/skypoint.py's STATUS_PRIORITY merge, which exists specifically
because THEIR sites reuse one id across a postponement.

DATE/TIME: the date bullet includes a real year ("Thursday, July 30,
2026") -- no year-inference/rollover trickery needed for the PRIMARY
date, unlike patriot.py/skypoint.py. The time bullet duplicates the
start/end time in two formats for accessibility ("9:00 AM 10:00 AM 09:00
10:00" -- 12hr start, 12hr end, 24hr start, 24hr end); TIME_RE below just
grabs the first 12-hour token as the start time.

The ONE place a year IS missing is inside "Continued to <date> at <time>"
status text (e.g. "Continued to October 1 at 4 PM") -- reusing patriot.py's
fuzzy-parse-plus-rollover approach for that one substring, since the
failure mode (year-boundary misparse) is identical. Rollover is checked
against the listing's own primary (pre-continuance) date rather than
"now" -- that's the correct reference for a continuance either way, and
sidesteps any drift between scrape time and the auction's original date.

IMPORTANT -- how this ties into base.py: base.py's scrape() unconditionally
runs `row["auction_dt"], row["timing"] = parse_auction_date(row["date_time"])`
on every row AFTER parse_listing() returns, for every spider, regardless of
what parse_listing put there. Two consequences, confirmed by reading
base.py directly (not just inferred from patriot.py/skypoint.py's own
row-level auction_dt/timing, which are likewise silently overwritten and
kept here only for internal use within this file, e.g. classify_timing()
calls, matching the existing precedent -- they never reach the final
output):

  1. The auction_dt/timing THIS spider computes are informational/internal
     only -- the real ones come from base.py re-parsing row["date_time"]
     as text via DATE_CHUNK_RE + dateutil, every time.
  2. base.py's parser has NO rollover logic (unlike this file's/patriot.py's
     own year-less-date handling) -- it just defaults a missing year to
     whatever `datetime.now()` is at scrape time. So row["date_time"]
     itself must never be handed off year-less, or a "Continued to
     October 1" scraped in, say, December could silently resolve to a
     year that's already almost over instead of rolling to next year.

The fix used throughout below: row["date_time"] is always built via
_format_date_chunk(), which renders a full datetime (year already
resolved using THIS file's own rollover logic where needed) into the
"Month D, YYYY at H:MM AM/PM" shape DATE_CHUNK_RE matches directly and
unambiguously -- so base.py's re-parse just confirms what was already
correctly computed here, rather than being the only thing computing it
with a weaker (no-rollover) method. See parse_auction_date()'s own
docstring in base.py: "Sites with a different date format ... should
write their own small extraction function" -- this is that, but handing
the *result* back in the shared chunk shape rather than bypassing
base.py's re-parse (parse_detail doesn't run for this spider, and
short-circuiting the shared field would mean carrying a second,
divergent code path forever for no benefit).

STATUS KEYWORDS (confirmed present in the fetched sample):
  "CANCELED" / "CANCELLED"        -> canceled (only "CANCELED" was
                                      actually observed on this site, but
                                      matching both spellings costs
                                      nothing and matches skypoint.py's
                                      defensive habit)
  "Currently going forward"       -> active, use the primary date/time
  "Continued to <date> at <time>" -> postponed, use the extracted date
  (none of the above)             -> NOT observed anywhere in the sample
                                      -- every row had exactly one of the
                                      three. Falls back to "active" using
                                      the primary date/time if this ever
                                      happens, same default-active
                                      fallback skypoint.py/patriot.py use
                                      for their own no-label case.

REGISTRY INFO: "Book NNNNN at Page NNN" is the common case; a few
listings use "Doc #NNNNNN, Cert of Title #CNN-NNN" instead (confirmed on
the 315 Allston St / condo listing). Neither is consistently present --
several rows have no registry line at all. Both are optional, folded into
extra_fields since there's no dedicated column for either on this site
(no "county" field anywhere in the listing text, unlike
sullivan.py/harmon.py).

ADDRESS PARSING: title text shape is "STREET[, more street detail...],
CITY[ (ALT NAME)], ST ZIP[ (NOTE)]" -- e.g. "111 Foster St, Apt. 510 a/k/a
Unit 510, Peabody, MA 01960" (street itself has an internal comma) or
"315 Allston St, Unit 11 a/k/a Unit 315-11, Brighton (Boston), MA 02135"
(city itself has a parenthetical) or "130 Rachael Ter, Westfield, MA
01085 (2ND MORTGAGE)" (trailing note AFTER the zip, not part of the
city). CITY_STATE_ZIP_RE below is anchored at the END of the string and
searches for the rightmost ", <city>, <ST> <ZIP>" with the city group's
character class explicitly excluding commas -- that's what lets it
correctly split all three shapes above without needing to guess how many
internal commas the street half contains.

PDF_LINKS: no PDF documents observed anywhere in the fetched sample --
pdf_links is still scanned for defensively (matches every other spider
here) but is expected to always come back empty for this site.
"""

import re
from datetime import datetime, timedelta
from urllib.parse import urljoin

from dateutil import parser as date_parser

from base import AuctionSpider, classify_timing, clean_url

EVENT_URL_RE = re.compile(r"/auction-calendar/(\d{4}/\d{1,2}/\d{1,2}/[^/?#\s]+)")
DATE_RE = re.compile(
    r"\b((?:Mon|Tues?|Wed(?:nes)?|Thu(?:rs)?|Fri|Sat(?:ur)?|Sun)[a-z]*,\s*"
    r"[A-Za-z]+\s+\d{1,2},\s*\d{4})\b"
)
TIME_RE = re.compile(r"\b(\d{1,2}:\d{2}\s*[AP]M)\b", re.IGNORECASE)
CANCELED_RE = re.compile(r"\bCANCEL(?:L)?ED\b", re.IGNORECASE)
GOING_FORWARD_RE = re.compile(r"Currently going forward", re.IGNORECASE)
CONTINUED_RE = re.compile(
    r"Continued to\s+(.+?)(?=\s+Book\s|\s+Doc\s*#|$)", re.IGNORECASE
)
BOOK_PAGE_RE = re.compile(r"Book\s+(\S+)\s+at\s+Page\s+(\S+)", re.IGNORECASE)
DOC_CERT_RE = re.compile(
    r"Doc\s*#\s*(\S+?)(?:,\s*Cert(?:ificate)?\s*of\s*Title\s*#\s*(\S+))?(?=\s|,|$)",
    re.IGNORECASE,
)
# Anchored at the end -- see module docstring's ADDRESS PARSING note for
# why the city group excludes commas rather than trying to count them.
CITY_STATE_ZIP_RE = re.compile(
    r",\s*([^,]+?),\s*(MA|NH|RI|VT|ME|CT)\s+(\d{5})\s*(?:\(([^)]+)\))?\s*$",
    re.IGNORECASE,
)


def _parse_continued_date(text, reference=None):
    """'Continued to <date> at <time>' text has no year -- same rollover
    fix as patriot.py's _parse_no_year_date, reused here since this is
    the only date on this site missing a year. `reference` doubles as
    both the year-default for the parse and the rollover check, same as
    patriot's version -- pass the listing's own primary date so the
    inferred year is anchored to the auction, not to scrape time."""
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


def _format_date_chunk(dt):
    """Render a resolved datetime into the 'Month D, YYYY at H:MM AM/PM'
    shape base.py's DATE_CHUNK_RE/parse_auction_date() expects (see
    module docstring's IMPORTANT note) -- this is what actually reaches
    the final output's auction_dt/timing, not this file's own auction_dt
    variables. Returns "" for None so callers can fall back to raw text
    when a datetime couldn't be resolved."""
    if dt is None:
        return ""
    return dt.strftime("%B %d, %Y at %I:%M %p")


def _parse_address(title):
    """Split a title like '111 Foster St, Apt. 510 a/k/a Unit 510,
    Peabody, MA 01960 (NOTE)' into (street, city_state, trailing_note).
    See module docstring's ADDRESS PARSING note."""
    m = CITY_STATE_ZIP_RE.search(title)
    if not m:
        return title.strip(), "", ""
    street = title[: m.start()].strip()
    city = m.group(1).strip()
    state = m.group(2).upper()
    zip_code = m.group(3)
    note = (m.group(4) or "").strip()
    return street, f"{city}, {state} {zip_code}", note


class LandmarkSpider(AuctionSpider):
    name = "landmark"
    base_url = "https://www.landmarkauction.biz"
    scrape_details = False  # confirmed: detail pages add nothing beyond the calendar page

    def listing_urls(self):
        return [f"{self.base_url}/"]

    def parse_listing(self, soup, listing_url):
        containers = soup.select("article.eventlist-event")  # ASSUMPTION #1
        if not containers:
            print(f"[{self.name}] 'article.eventlist-event' matched nothing -- "
                  f"falling back to signature-based grouping. Verify output "
                  f"carefully -- see module docstring ASSUMPTION #1.")
            containers = self._fallback_containers(soup)

        rows = []
        seen_ids = set()
        for container in containers:
            row = self._parse_event(container)
            if not row:
                continue
            if row["id"] in seen_ids:
                continue  # shouldn't happen (URLs are inherently unique) but don't double-count
            seen_ids.add(row["id"])
            rows.append(row)
        return rows

    def _fallback_containers(self, soup):
        """
        Reconstruct one "container" per event when the primary selector
        finds nothing. For each unique event-detail link, climbs its DOM
        ancestors (capped at 8 levels) until the accumulated text
        contains both an <h1> and a recognizable status/date signature --
        same content-signature philosophy as skypoint.py's
        _looks_like_listing(). The depth cap keeps a bug here from
        silently swallowing the whole page as one "container".
        """
        seen_keys = set()
        containers = []
        for a in soup.find_all("a", href=True):
            m = EVENT_URL_RE.search(a["href"])
            if not m:
                continue
            key = m.group(1)
            if key in seen_keys:
                continue
            node = a
            depth = 0
            while depth < 8:
                text = node.get_text(" ", strip=True)
                has_signature = (
                        CANCELED_RE.search(text)
                        or GOING_FORWARD_RE.search(text)
                        or CONTINUED_RE.search(text)
                        or BOOK_PAGE_RE.search(text)
                )
                if has_signature and node.find("h1"):
                    break
                if node.parent is None:
                    break
                node = node.parent
                depth += 1
            seen_keys.add(key)
            containers.append(node)
        return containers

    def _parse_event(self, container):
        h1 = container.find("h1")
        if not h1:
            return None

        # The link carrying the real href might wrap the h1 or sit inside
        # it -- handle both without assuming which way Squarespace nests
        # them (see module docstring ASSUMPTION #1).
        url_anchor = (
                h1.find("a", href=True)
                or h1.find_parent("a", href=True)
                or container.select_one("a[href*='/auction-calendar/']")
        )
        if not url_anchor:
            return None

        m = EVENT_URL_RE.search(url_anchor["href"])
        if not m:
            return None
        event_id = m.group(1)
        url = urljoin(self.base_url, url_anchor["href"])

        title = h1.get_text(" ", strip=True)
        street, city_state, note = _parse_address(title)

        text = container.get_text(" ", strip=True)

        date_match = DATE_RE.search(text)
        primary_date_text = date_match.group(1) if date_match else ""
        time_search_from = date_match.end() if date_match else 0
        time_match = TIME_RE.search(text, time_search_from)
        start_time_text = time_match.group(1) if time_match else ""

        primary_dt = None
        if primary_date_text:
            try:
                primary_dt = date_parser.parse(
                    f"{primary_date_text} {start_time_text}".strip(), fuzzy=True
                )
            except (ValueError, OverflowError):
                primary_dt = None

        if CANCELED_RE.search(text):
            status = "canceled"
            auction_dt = primary_dt
            date_time = _format_date_chunk(auction_dt) or primary_date_text
        else:
            continued_match = CONTINUED_RE.search(text)
            if continued_match:
                status = "postponed"
                continued_text = continued_match.group(1).strip()
                # Resolve the year HERE (rollover-aware) rather than
                # handing continued_text's bare "October 1 at 4 PM" off
                # to base.py's own (rollover-less) parser -- see module
                # docstring's IMPORTANT note.
                auction_dt = _parse_continued_date(continued_text, reference=primary_dt)
                date_time = _format_date_chunk(auction_dt) or continued_text
            else:
                # Covers "Currently going forward" and the (unobserved)
                # no-label case -- see module docstring STATUS KEYWORDS.
                status = "active"
                auction_dt = primary_dt
                date_time = (
                        _format_date_chunk(auction_dt)
                        or f"{primary_date_text} {start_time_text}".strip()
                )

        timing = classify_timing(auction_dt) if auction_dt else "Unknown"

        extra_parts = []
        bp_match = BOOK_PAGE_RE.search(text)
        if bp_match:
            extra_parts.append(f"Book/Page: {bp_match.group(1)}/{bp_match.group(2)}")
        else:
            doc_match = DOC_CERT_RE.search(text)
            if doc_match:
                doc_part = f"Doc #: {doc_match.group(1)}"
                if doc_match.group(2):
                    doc_part += f", Cert of Title #: {doc_match.group(2)}"
                extra_parts.append(doc_part)
        if note:
            extra_parts.append(note)
        if status == "postponed" and primary_date_text:
            extra_parts.append(f"Originally scheduled: {primary_date_text}")

        pdf_links = [
            clean_url(urljoin(self.base_url, a["href"]))
            for a in container.find_all("a", href=True)
            if a["href"].lower().endswith(".pdf")
        ]

        return {
            "id": event_id,
            "url": url,
            "date_time": date_time,
            "auction_dt": auction_dt,
            "timing": timing,
            "status": status,
            "street": street,
            "city_state": city_state,
            "description": "",  # site gives no separate description beyond the address/status
            "extra_fields": " | ".join(extra_parts),
            "pdf_links": "; ".join(pdf_links),
        }