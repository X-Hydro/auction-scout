"""
Keenan Auction Company (keenanauction.com) -- Maine real estate auctions.

Real estate only (list.cgi?t=1); equipment (list.cgi?t=2) is out of
scope. robots.txt is unconfirmed -- couldn't fetch it directly. Verify
before running if that matters.

LIST PAGE: each listing lives in one <span id="BODYcopy"> holding the
auction link, a no-year date, and a <b> block with title + address,
optionally followed by red-flag <font> tags ("POSTPONED", "NEW DATE",
"PIP AVAILABLE") -- only "POSTPONED" affects status; the rest land in
extra_fields. Address formatting is inconsistent in the source itself
(comma vs no comma between street/city; no address at all for portfolio
listings) -- _split_address() degrades rather than guesses.

DETAIL PAGE: each line is its own <span id="BODYcopy">. parse_detail()
searches for the "Real Estate Foreclosure Auction..." header line rather
than assuming it's line 0, since some pages (portfolio index pages) have
preamble content first. The 5 lines after the header are auction#/title/
street/city/date; everything after is label:value text (Real Estate/
Preview/Directions/Terms).

PORTFOLIO LISTINGS: some list entries are an INDEX over several separate
real auctions, not a property themselves (e.g. Auction 26-63/i=5641, a
10-parcel portfolio). parse_listing() fetches every listing's detail
page during discovery and expands a portfolio page
(_extract_sub_listings()) into one row per real sub-listing instead of
one ungeocodable row for the index itself. Sub-listing addresses have no
state suffix in the source, so ", Maine" is appended before splitting.

SUMMARY / DESCRIPTION / METADATA:
- row["description"] (CSV "Summary") is the short list-page title --
  parse_detail() never overwrites it with the longer "Real Estate:"
  paragraph.
- extra_fields (CSV "Description") holds Preview/Directions/Terms and
  the PIP info-request link.
- metadata (CSV "Metadata") holds the AI's own extra_fields output
  (style, frontage, rooms, tax map, etc.) reshaped into real JSON via
  _extra_fields_to_json(). Never overlaps property_type/bedrooms/etc --
  the AI's system prompt already excludes those from extra_fields.

AI EXTRACTION runs unconditionally on every listing -- there's no
non-AI mode here (unlike sullivan/jjmanning/patriot). The "Real Estate:"
paragraph is the only place property specs exist as prose; listings
without one (confirmed: roughly half of real Keenan listings --
commercial/land pages use a different bulleted-field template instead)
just get blank spec columns, not an error. A failed/missing
ANTHROPIC_API_KEY degrades to blank specs rather than breaking the run.
"""

import copy
import json
import re
from datetime import datetime, timedelta
from urllib.parse import urljoin

from dateutil import parser as date_parser

from base import AuctionSpider, classify_timing, clean_url
from ai_property_extractor import extract_property_specs, PropertySpecs

LABELS = ("Real Estate", "Preview", "Directions", "Terms")
_LABEL_RE = re.compile(r"^(" + "|".join(re.escape(l) for l in LABELS) + r"):\s*(.*)$")


def _parse_no_year_date(text, reference=None):
    """List-page dates have no year -- parse against `reference`'s year,
    then roll forward a year if that lands >30 days in the past (handles
    scrapes near a year boundary)."""
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


def _date_text_after_link(a_tag):
    """Plain text between the auction link and the next <br> -- walks
    siblings rather than trusting next_sibling to be exactly right."""
    parts = []
    for sib in a_tag.next_siblings:
        if getattr(sib, "name", None) == "br":
            break
        parts.append(sib if isinstance(sib, str) else sib.get_text())
    return "".join(parts).strip()


def _lines_from_br(tag):
    """Text lines inside `tag`, split on <br>. Operates on a deep copy
    so the original soup (read again elsewhere) isn't disturbed."""
    clone = copy.deepcopy(tag)
    for br in clone.find_all("br"):
        br.replace_with("\n")
    return [ln.strip() for ln in clone.get_text().split("\n") if ln.strip()]


def _split_address(blob):
    """Best-effort street/city/state split -- the source is inconsistent
    (comma count varies; portfolio listings have no real address at
    all), so this degrades rather than guesses:
      - 2+ commas: clean 3-way split
      - 1 comma: street kept whole, city left blank
      - 0 commas: whole blob kept as street
    """
    if not blob:
        return "", "", ""
    parts = [p.strip() for p in blob.rsplit(",", 2)]
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return parts[0], "", parts[1]
    return blob.strip(), "", ""


def _extract_labeled_lines(lines):
    """Map of label -> value for any LABELS found at the start of a
    line. A missing label just doesn't appear in the result."""
    found = {}
    for line in lines:
        m = _LABEL_RE.match(line)
        if m:
            found[m.group(1)] = m.group(2).strip()
    return found


def _extra_fields_to_json(extra_fields):
    """Converts the AI's 'Label: value; Label: value' extra_fields
    string into a dict for metadata_json. Keys are slugified from
    whatever labels the model used -- there's no fixed label set to
    look up against."""
    if not extra_fields:
        return {}
    result = {}
    for part in extra_fields.split("; "):
        if ": " not in part:
            continue
        label, _, value = part.partition(": ")
        key = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
        value = value.strip()
        if key and value:
            result[key] = value
    return result


_HEADER_RE = re.compile(r"^Real Estate Foreclosure Auction\b", re.IGNORECASE)
_PARCEL_LABEL_RE = re.compile(r"To View Auction Parcel\s+(\d+)\s*-\s*(.+)$", re.IGNORECASE)

# Some detail pages (confirmed: sequential-slot parcels within a
# portfolio auction) show a start-end time range on the date line, e.g.
# "October 7, 2026 10:30 AM - 11:30 AM", rather than a bare timestamp.
# dateutil's fuzzy parser doesn't reject the trailing "- 11:30" -- it
# silently reads it as a UTC offset instead of an end time, producing a
# garbage tz-aware datetime. Strip a trailing time-range tail before
# parsing so only the (real) start time reaches dateutil.
_TIME_RANGE_TAIL_RE = re.compile(r"\s*-\s*\d{1,2}:\d{2}\s*(?:AM|PM)?\s*$", re.IGNORECASE)

# Confirmed on portfolio sub-listing/parcel detail pages: the position
# where the real auction date normally sits instead holds a "PREVIEW
# DATE: ..." announcement (a property-viewing window, not the auction).
# This isn't a dateutil parse failure -- fuzzy-parsing a preview line
# still "succeeds", just against the wrong event -- so it has to be
# caught explicitly rather than left to the try/except below.
_PREVIEW_LINE_RE = re.compile(r"^\s*PREVIEW\b", re.IGNORECASE)

# The reliable signal, when present: an explicit "AUCTION DATE:" line
# (colon spacing and case both vary -- "AUCTION DATE:Wed...", "AUCTION
# DATE: Wed...", "Auction Date: ~~Wed...~~ POSTPONED..." all appear).
# Confirmed to sit at very different positions across listing templates
# (portfolio parcel pages add extra lines -- "Auction Parcel #N", a
# 2-line description -- before it), so this is searched for across
# every line after the header rather than trusted at a fixed index.
_AUCTION_DATE_LINE_RE = re.compile(r"^AUCTION DATE:?\s*(.*)$", re.IGNORECASE)

# Per Thale: this exact phrase (or a close variant using the same four
# words in order, e.g. "postponed until further notice due to a
# Bankruptcy Filing") appearing ANYWHERE on the page means the auction
# is effectively cancelled. It can sit on its own line, separate from
# the date line itself, so this is checked against the whole page text
# rather than tied to whatever line the date came from.
_POSTPONED_TEXT_RE = re.compile(r"POSTPONED\s+UNTIL\s+FURTHER\s+NOTICE", re.IGNORECASE)

# Cuts a trailing "POSTPONED ..." notice off of an auction-date line
# once it's served its purpose as a cancellation signal above -- it
# isn't part of the date and would otherwise ride along into the
# parser as fuzzy-parser noise.
_POSTPONED_CUTOFF_RE = re.compile(r"\s*POSTPONED\b.*", re.IGNORECASE)

# Some "postponed" listings actually give a specific new date rather than
# an indefinite hold, e.g. "Auction is being postponed until Friday,
# October 16th at 11AM." -- distinct from the original (often
# strikethrough) date elsewhere on the page, which is now stale. Checked
# ahead of everything else: when the text after "postponed until" parses
# as a real date, that's the current, authoritative one and the listing
# is NOT cancelled. When it doesn't parse (e.g. "...until further notice
# due to a Bankruptcy Filing"), this naturally falls through to the
# indefinite-postponement handling below -- no separate case needed to
# tell the two apart.
_RESCHEDULE_RE = re.compile(r"postponed\s+until\s+(.+)", re.IGNORECASE)


def _format_auction_dt(dt):
    """'Wed, Oct 21 at 3:00 PM' -- matches the plain format Keenan's own
    list-page dates already use. Avoids %-d/%-I (unsupported on Windows)."""
    return f"{dt.strftime('%a, %b')} {dt.day} at {dt.strftime('%I:%M %p').lstrip('0')}"


def _extract_sub_listings(soup, own_id):
    """
    Detects a portfolio index page: a sequence of "To View Auction
    Parcel N - <address>" lines, each immediately followed by a line
    holding a link to a DIFFERENT auction.cgi?i=<id> (own_id excluded).
    Returns (parcel_num, address, sub_id, sub_href) tuples, empty if
    this isn't a portfolio page.
    """
    results = []
    pending = None  # (parcel_num, address_text) waiting for its link span
    for span in soup.find_all("span", id="BODYcopy"):
        text = span.get_text(" ", strip=True)
        label_match = _PARCEL_LABEL_RE.search(text)
        if label_match:
            pending = (label_match.group(1), label_match.group(2).strip())
            continue
        if pending:
            a = span.find("a", href=re.compile(r"auction\.cgi\?i=\d+", re.IGNORECASE))
            if a:
                m = re.search(r"i=(\d+)", a["href"])
                sub_id = m.group(1) if m else None
                if sub_id and sub_id != str(own_id):
                    results.append((pending[0], pending[1], sub_id, a["href"]))
            pending = None
    return results


class KeenanAISpider(AuctionSpider):
    name = "keenan"
    base_url = "https://keenanauction.com"
    scrape_details = True

    def listing_urls(self):
        return [f"{self.base_url}/list.cgi?t=1"]

    def parse_listing(self, soup, listing_url):
        rows = []
        for span in soup.find_all("span", id="BODYcopy"):
            a = span.find("a", href=True)
            if not a or "auction.cgi" not in a["href"]:
                continue

            m = re.search(r"i=(\d+)", a["href"])
            auction_id = m.group(1) if m else None
            if not auction_id:
                continue

            auction_num = re.sub(
                r"^Auction\s+", "", a.get_text(strip=True), flags=re.IGNORECASE
            )

            date_text = _date_text_after_link(a)
            auction_dt = _parse_no_year_date(date_text)
            timing = classify_timing(auction_dt) if auction_dt else "Unknown"

            bold_tags = span.find_all("b")
            title, address_blob = "", ""
            if bold_tags:
                lines = _lines_from_br(bold_tags[0])
                title = lines[0] if lines else ""
                address_blob = lines[1] if len(lines) > 1 else ""

            street, city, state = _split_address(address_blob)
            city_state = f"{city}, {state}".strip(", ")

            flags = [f.get_text(strip=True) for f in span.select("font[color=red]")]
            flags = [f for f in flags if f]
            status = "postponed" if any("POSTPONED" in f.upper() for f in flags) else "active"

            extra_parts = [f"Auction #: {auction_num}"] + flags
            url = urljoin(self.base_url, a["href"])

            # Some listings are an INDEX over several separate real
            # auctions rather than a property themselves -- checked on
            # every listing (not just suspicious-looking ones) via an
            # extra detail-page fetch; cheap insurance on a ~10-listing
            # site against silently treating a whole portfolio as one
            # bogus, ungeocodable row.
            try:
                detail_soup = self.get_soup(url)
                sub_listings = _extract_sub_listings(detail_soup, auction_id)
            except Exception as e:
                print(f"[{self.name}] portfolio-check fetch failed for {url}: "
                      f"{type(e).__name__}: {e} -- treating as a normal listing")
                sub_listings = []

            if sub_listings:
                print(f"[{self.name}] {url} is a {len(sub_listings)}-parcel "
                      f"portfolio listing (Auction {auction_num}) -- "
                      f"expanding into one row per parcel")
                for parcel_num, parcel_address, sub_id, sub_href in sub_listings:
                    # Parcel addresses have no state ("<street>, <city>")
                    # -- appending ", Maine" reuses _split_address()
                    # correctly instead of misfiling city into state.
                    sub_street, sub_city, sub_state = _split_address(
                        f"{parcel_address}, Maine"
                    )
                    rows.append({
                        "id": sub_id,
                        "url": urljoin(self.base_url, sub_href),
                        "date_time": date_text,
                        "auction_dt": auction_dt,
                        "timing": timing,
                        "status": status,
                        "street": sub_street,
                        "city_state": f"{sub_city}, {sub_state}".strip(", "),
                        "description": f"{title} -- Parcel {parcel_num}",
                        "extra_fields": "; ".join(
                            p for p in extra_parts + [
                                f"Part of portfolio auction {auction_num} "
                                f"({len(sub_listings)} parcels)"
                            ] if p
                        ),
                        "pdf_links": "",
                    })
                continue  # the index page itself isn't a real property

            rows.append({
                "id": auction_id,
                "url": url,
                "date_time": date_text,
                "auction_dt": auction_dt,
                "timing": timing,
                "status": status,
                "street": street,
                "city_state": city_state,
                "description": title,
                "extra_fields": "; ".join(p for p in extra_parts if p),
                "pdf_links": "",
            })
        return rows

    def parse_detail(self, soup, row):
        lines = [
            s.get_text(" ", strip=True)
            for s in soup.find_all("span", id="BODYcopy")
        ]
        lines = [ln for ln in lines if ln]

        # Search for the header rather than assuming lines[0] -- some
        # pages have preamble content before it.
        header_idx = next(
            (i for i, ln in enumerate(lines) if _HEADER_RE.match(ln)), None
        )
        if header_idx is None or len(lines) < header_idx + 5:
            return {}
        lines = lines[header_idx:]

        result = {}

        # Check for a specific-date reschedule first -- it supersedes any
        # other date on the page (including a struck-through original) and
        # means the listing is NOT cancelled, so nothing below should run
        # if this succeeds.
        reschedule_dt = None
        for ln in lines[1:]:
            m = _RESCHEDULE_RE.search(ln)
            if not m:
                continue
            candidate_text = _TIME_RANGE_TAIL_RE.sub("", m.group(1))
            try:
                candidate_dt = date_parser.parse(candidate_text, fuzzy=True)
            except (ValueError, OverflowError):
                continue
            if candidate_dt.tzinfo is not None:
                candidate_dt = candidate_dt.replace(tzinfo=None)
            reschedule_dt = candidate_dt
            break

        if reschedule_dt is not None:
            result["date_time"] = _format_auction_dt(reschedule_dt)
            result["auction_dt"] = reschedule_dt
            result["timing"] = classify_timing(reschedule_dt)
        else:
            # "POSTPONED UNTIL FURTHER NOTICE" (or a close variant) anywhere
            # on the page means the auction is effectively cancelled --
            # checked independent of the date line, since it can sit on its
            # own line.
            if _POSTPONED_TEXT_RE.search(" ".join(lines)):
                result["status"] = "postponed"

            # Prefer an explicit "Auction Date:" line wherever it appears --
            # unambiguous, and confirmed to sit at very different positions
            # across templates (portfolio parcel pages insert extra lines --
            # "Auction Parcel #N", a 2-line description -- before it, which
            # is exactly what was landing the street address itself in
            # "date_time" under the old fixed-index approach).
            auction_date_text = None
            for ln in lines[1:]:
                m = _AUCTION_DATE_LINE_RE.match(ln)
                if m:
                    auction_date_text = m.group(1)
                    break

            # Fall back to the old fixed-index heuristic only for templates
            # with no explicit label at all (confirmed to exist alongside
            # the labeled ones -- e.g. some single-property listings just
            # show a bare date). Still guard against PREVIEW DATE landing
            # there.
            if auction_date_text is None and len(lines) > 4 and not _PREVIEW_LINE_RE.match(lines[4]):
                auction_date_text = lines[4]

            if auction_date_text:
                # Strip literal strikethrough markers if present, and drop
                # any trailing "POSTPONED ..." notice riding along on the
                # same line -- it already did its job above and isn't part
                # of the date. Also guard against a trailing time range
                # (e.g. "10:30 AM - 11:30 AM") that dateutil's fuzzy parser
                # can misread as a UTC offset instead of an end time.
                cleaned = auction_date_text.replace("~", " ")
                cleaned = _POSTPONED_CUTOFF_RE.sub("", cleaned)
                cleaned = _TIME_RANGE_TAIL_RE.sub("", cleaned)
                try:
                    detail_dt = date_parser.parse(cleaned, fuzzy=True)
                    if detail_dt.tzinfo is not None:
                        # Keenan never publishes a real UTC offset. Any
                        # tzinfo here means dateutil misread something as
                        # an offset.
                        detail_dt = detail_dt.replace(tzinfo=None)
                    result["date_time"] = _format_auction_dt(detail_dt)
                    result["auction_dt"] = detail_dt
                    result["timing"] = classify_timing(detail_dt)
                except (ValueError, OverflowError):
                    pass

        labeled = _extract_labeled_lines(lines[5:])
        description = labeled.get("Real Estate", "")
        # result["description"] is deliberately not set here -- the row
        # already carries a short title from parse_listing(), which is
        # more useful as a glanceable summary than this full paragraph.
        # `description` (local var) still feeds the AI call below.

        if description:
            try:
                specs = extract_property_specs(
                    description,
                    source_site=self.name,
                    cache_key=f"{self.name}:{row['id']}",
                )
            except Exception as e:
                print(f"[{self.name}] AI property extraction failed on {row['url']}: {e}")
                specs = PropertySpecs()
            result["property_type"] = specs.property_type
            result["bedrooms"] = specs.bedrooms
            result["bathrooms"] = specs.bathrooms
            result["sqft"] = specs.sqft
            result["lot_size"] = specs.lot_size
            result["year_built"] = specs.year_built
            if specs.extra_fields:
                metadata = _extra_fields_to_json(specs.extra_fields)
                if metadata:
                    result["metadata"] = json.dumps(metadata)

        extra_parts = []
        for label in ("Preview", "Directions", "Terms"):
            if label in labeled:
                extra_parts.append(f"{label}: {labeled[label]}")

        pip_link = soup.find("a", href=re.compile(r"getinfo\.cgi", re.IGNORECASE))
        if pip_link and pip_link.get("href"):
            extra_parts.append(
                f"Info Request: {urljoin(self.base_url, pip_link['href'])}"
            )

        if extra_parts:
            result["extra_fields"] = "; ".join(extra_parts)

        pdf_links = [
            clean_url(urljoin(self.base_url, a["href"]))
            for a in soup.find_all("a", href=True)
            if a["href"].lower().endswith(".pdf")
        ]
        if pdf_links:
            result["pdf_links"] = "; ".join(pdf_links)

        return result