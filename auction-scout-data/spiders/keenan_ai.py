"""
Keenan Auction Company (keenanauction.com) -- Maine real estate auctions.

Real estate only (list.cgi?t=1). Equipment auctions (list.cgi?t=2) are
explicitly out of scope per instruction -- not real estate, not handled
here, and this file doesn't try to be generic across both.

ROBOTS.TXT: not confirmed directly -- keenanauction.com/robots.txt couldn't
be fetched to check. Google has both list.cgi and auction.cgi indexed
(found via search), suggesting they aren't blocked for Googlebot at least,
but that's not proof for other agents. Check /robots.txt yourself before
running this. respect_robots is deliberately left unset (base.py's normal
fail-closed default) rather than assumed clear -- if it turns out to be
disallowed, that needs the same kind of explicit go-ahead documented in
harmon.py before flipping it off, not just a guess.

LISTING PAGE (list.cgi?t=1): confirmed from a real fetch (2026-08-05, 10
listings). Every listing is fully contained in one non-unique
`<span id="BODYcopy">` (old hand-coded markup, duplicate ids throughout the
site -- selecting by id works anyway since BeautifulSoup doesn't enforce
uniqueness):

    <span id="BODYcopy"><a href="auction.cgi?i=5683">Auction 26-111</a>
    Wed, Sep 2 at 02:00 PM<br>
    <b>2BR Cape Style Home - 1+/- Acres<br>
    32 Dolloff Rd., Sebago, Maine</b></span>

...optionally followed, still inside the same span, by one or more
`<b><font color=red>FLAG</font></b>` tags. Confirmed flag text seen:
"POSTPONED", "NEW DATE", "PIP AVAILABLE" (a Property Info Package is
available -- not a status). Only "POSTPONED" is treated as a status here;
the rest are informational and kept in extra_fields. NOT CONFIRMED: what a
cancelled listing looks like on this page -- no example seen in the one
fetch. If one shows up and isn't handled right, tell me the actual
markup/text and I'll fix the detection.

Address formatting is genuinely inconsistent in the source itself -- not a
parsing bug, the site really does mix these:
  - most listings: "<street>, <city>, Maine" (2+ commas)
  - at least 3 of the 10 real listings seen: "<street> <city>, Maine" --
    NO comma between street and city ("590 Main St. Dixfield, Maine")
  - one multi-town portfolio listing: no address at all, just
    "Portland - Westbrook - Lewiston - Maine" (dash-joined, zero commas)
_split_address() degrades gracefully across these rather than guessing at
a split that isn't actually present in the source -- see its docstring.

Date/time on the list page has NO YEAR ("Wed, Sep 2 at 02:00 PM") -- same
issue as patriotauctioneers.com, handled the same way (_parse_no_year_date:
dateutil fuzzy parse + roll forward a year if that lands >30 days in the
past).

DETAIL PAGE (auction.cgi?i=<id>): confirmed against exactly ONE real page
(i=5683 / Auction 26-111, 2026-08-05) -- this is a real limitation, unlike
Sullivan/Harmon's detail parsing this hasn't been checked against a
land-only listing, a portfolio listing, or one missing a "Real Estate:"
section. Structure: unlike the listing page, here each *line* is its own
`<span id="BODYcopy">`, with `<br>` tags sitting between spans as
separators rather than inside them -- so `find_all("span", id="BODYcopy")`
yields one line per element, in order:

    line 0: "Real Estate Foreclosure Auction 26-111"
    line 1: title
    line 2: street (trailing comma)
    line 3: "city, Maine"
    line 4: full date WITH year ("Wednesday, September 2, 2026 at 2PM")
    ...then labeled free-text lines: "Real Estate:", "Preview:",
    "Directions:", "Terms:" all confirmed present on the one page seen.
    "Buyer Broker Participation Available." also appears but as its own
    bolded sentence with no colon -- not a "Label:" line, so it is NOT
    extracted (would need a different, unconfirmed pattern to capture).

Line 4's date has a year, unlike the list page's -- preferred as
authoritative when it parses, same "detail page refines list page" pattern
as patriot.py's parse_detail().

A "PIP" (Property Information Package) request link (`getinfo.cgi?a=...`)
appeared on the one detail page seen, even though that particular listing
(26-111) was NOT flagged "PIP AVAILABLE" on the list page -- unclear
whether every real-estate detail page has this link or just some;
captured opportunistically when present into `result["metadata"]` as
JSON (`{"pip_link": "..."}`) -- see schema.sql's metadata_json column --
flagged here as unconfirmed rather than asserted as a rule.

PORTFOLIO LISTINGS: confirmed on Auction 26-63 (i=5641, 2026-08-06) -- a
single list-page entry ("(12) Multi-Unit Residential & Commercial
Buildings- (134) Units", address "Portland - Westbrook - Lewiston -
Maine") whose own detail page isn't a property at all, just an INDEX over
10 separate real auctions (i=5630-5639), each with its own real,
geocodable address. Detected in parse_listing() via _extract_sub_listings()
-- every listing's detail page gets fetched during discovery (not just
suspicious-looking ones) looking for "To View Auction Parcel N - <address>
/ Click Here" link pairs; if found, that one row is replaced by one row
per linked sub-listing instead of kept as a single bogus, ungeocodable
row. Each sub-row then goes through the normal per-listing parse_detail()
path exactly like any other listing. NOT CONFIRMED: whether i=5630-5639
actually follow the same single-property template as the one confirmed
plain page (i=5683) -- proceeding on that assumption since portfolio
sub-listings are advertised as "individual property web pages" separate
from the index, but if their fields come back wrong/empty, that's the
first thing to check.

AI EXTRACTION IS ALWAYS ON for this spider, not optional. Unlike sullivan/
jjmanning/patriot (which stay plain/no-AI -- their sites are structured
enough that the useful fields come from regex, and an earlier AI attempt
for them was retired), this page's "Real Estate:" paragraph is the ONLY
place property_type/bedrooms/bathrooms/sqft/lot_size/year_built exist at
all, as free prose with zero structured markup behind it. There's no
meaningfully different "plain" version of this spider to fall back to --
every other field (address/date/status/Preview/Directions/Terms/pdf_links)
already comes from parse_listing()/regex either way, so a no-AI mode would
just mean permanently blank spec columns, not a cheaper equivalent. A
failed/missing ANTHROPIC_API_KEY degrades gracefully to blank spec fields
(see parse_detail()) rather than breaking the run, but there's no flag to
turn AI off on purpose here -- if that's ever wanted, ask and I'll add one.

KNOWN GAP, ACCEPTED (2026-08-06): investigated with real detail-page
source for i=5670/5673 and several i=563x portfolio sub-listings --
turns out this site has AT LEAST 3 distinct detail-page templates, not
one template with occasional label gaps:
  1. Plain residential (i=5683, i=5682, i=5667 confirmed) -- "Real Estate
     Foreclosure Auction..." header, then a "Real Estate:" prose
     paragraph. What this file was originally built around, and the
     ONLY shape AI extraction runs on.
  2. Commercial/land (i=5670, i=5673, i=5672, i=5671, i=5627 confirmed)
     -- header says "Real Estate Auction..." (no "Foreclosure"),
     "AUCTION DATE:" instead of a bare date line, NO "Real Estate:"
     paragraph at all -- instead a narrative paragraph followed by
     bulleted "* LOT SIZE:" / "* GROSS BUILDING AREA:" fields.
  3. Portfolio sub-listings (i=5630-5639 confirmed) -- yet another shape:
     "Auction Parcel #N" line, sometimes a neighborhood line, street+city
     combined on one line, "Site"/"Improvements" bullet sections with
     specs embedded in prose ("2BR,1BTH 1,189+/-SF").
Decided NOT to chase per-template label variants further (confirmed to
be an open-ended whack-a-mole -- every new page pasted revealed another
shape) or to fall back to raw page text for the ones that don't match --
every row already carries `url` pointing at the real listing, so a
human who wants that content can just click through rather than the
pipeline duplicating it. Templates 2 and 3 will have blank
property_type/bedrooms/etc.; that's accepted, not pursued further.

SUMMARY LABEL (2026-08-06, cheap partial mitigation for the gap above):
row["description"] is a short, genuinely useful one-line label for every
listing regardless of template -- parse_listing() already captures it
from the list page's title line (e.g. "2BR Cape Style Home - 1+/- Acres",
"31-Unit Apartment Building"), no AI or extra parsing involved. The only
thing that was wrong: parse_detail() used to overwrite it with the full
"Real Estate:" paragraph for template-1 listings specifically (the ones
WITH that section) -- inconsistent (template 1 lost its short label,
templates 2/3 never did) and traded a glanceable summary for a whole
paragraph. Fixed by no longer writing description in parse_detail() at
all; the short title from parse_listing() now persists untouched for
every listing. Local `description` var there still feeds the AI call.
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
    """Keenan's list-page dates have no year ("Wed, Sep 2 at 02:00 PM") --
    same issue as patriotauctioneers.com, same fix: parse assuming
    `reference`'s year, then roll forward a year if that lands the date
    more than 30 days in the past (handles auctions scraped near a year
    boundary)."""
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
    """Plain text between the auction link and the first <br> in this
    listing's span -- e.g. "Wed, Sep 2 at 02:00 PM". Walks siblings
    rather than trusting a_tag.next_sibling to be exactly the text node,
    in case a stray whitespace-only node intervenes."""
    parts = []
    for sib in a_tag.next_siblings:
        if getattr(sib, "name", None) == "br":
            break
        parts.append(sib if isinstance(sib, str) else sib.get_text())
    return "".join(parts).strip()


def _lines_from_br(tag):
    """Text lines inside `tag`, split on <br> -- the source stacks fields
    (title / address) on separate visual lines with nothing else marking
    the boundary. Operates on a deep copy so it doesn't disturb the
    original soup (the same span gets read again for flags)."""
    clone = copy.deepcopy(tag)
    for br in clone.find_all("br"):
        br.replace_with("\n")
    return [ln.strip() for ln in clone.get_text().split("\n") if ln.strip()]


def _split_address(blob):
    """Best-effort split of the list page's single address line into
    (street, city, state). Confirmed inconsistent IN THE SOURCE ITSELF --
    see module docstring. Degrades rather than guesses:
      - 2+ commas: clean split via rsplit(',', 2) -- (street, city, state)
      - exactly 1 comma: can't tell where street ends and city begins, so
        the remainder before the comma is kept whole as `street`, city
        left blank
      - 0 commas (e.g. the dash-joined multi-town portfolio listing): the
        whole blob is kept as `street`, city/state both blank
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
    """Map of label -> value for any of LABELS found at the start of a
    line, among the free-text lines following the detail page's fixed
    5-line header. Labels not present just don't show up in the result --
    tolerated as a miss (matches sullivan.py's _extract_property_details
    philosophy) rather than treated as an error, since only one detail
    page has ever been confirmed and other listing types may omit some."""
    found = {}
    for line in lines:
        m = _LABEL_RE.match(line)
        if m:
            found[m.group(1)] = m.group(2).strip()
    return found


_HEADER_RE = re.compile(r"^Real Estate Foreclosure Auction\b", re.IGNORECASE)
_PARCEL_LABEL_RE = re.compile(r"To View Auction Parcel\s+(\d+)\s*-\s*(.+)$", re.IGNORECASE)


def _extract_sub_listings(soup, own_id):
    """
    Detects a Keenan "portfolio" detail page -- one whose body is really
    an INDEX over several separate real auctions, not a property of its
    own. Confirmed on Auction 26-63 (i=5641, 2026-08-06): a 10-parcel
    portfolio across Portland/Westbrook/Lewiston, whose body has no
    "Real Estate:" section at all -- instead a sequence of
    "To View Auction Parcel N - <address>" label lines, each immediately
    followed by a line holding a "Click Here." link to a DIFFERENT
    auction.cgi?i=<id> (own_id excluded, in case a page ever linked back
    to itself). Each of those ids is a normal single-property page.

    Returns a list of (parcel_num, address_text, sub_id, sub_href)
    tuples, empty if this isn't a portfolio page. Matching is by strict
    adjacency (label line, then the very next span) -- confirmed exact
    on the one page seen (no blank filler line between a label and its
    link), so no lookahead/fuzzy matching was needed.
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
    scrape_details = True  # list page has enough for a bare row, but detail
    # has the year-qualified date plus Real Estate/Preview/Terms text

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

            # Some listings are really an INDEX over several separate
            # real auctions, not a property themselves -- confirmed on
            # Auction 26-63 (i=5641, 2026-08-06): a 10-parcel portfolio
            # across Portland/Westbrook/Lewiston, whose own address blob
            # was un-splittable ("Portland - Westbrook - Lewiston -
            # Maine", the zero-comma case _split_address() already had
            # to degrade for) precisely because it isn't a real address
            # at all -- see _extract_sub_listings(). Checked for on
            # EVERY listing here (an extra detail-page fetch per row
            # during discovery, not just ones that look suspicious) --
            # same "run it every time" tradeoff harmon.py's ID-range
            # probing makes: a handful of extra requests on a ~10-listing
            # site is cheap insurance against silently treating a whole
            # portfolio as one bogus, ungeocodable row instead of the
            # real properties inside it.
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
                    # Parcel addresses are "<street>, <city>" -- no state
                    # at all (unlike the list page's "<street>, <city>,
                    # Maine"). Appending ", Maine" before the same split
                    # logic both fixes the format mismatch (reusing
                    # _split_address() unmodified on a 2-part string would
                    # misfile the city into the state slot) and matters
                    # for real: without an explicit state, the geocoded
                    # address string would just be "<street>, Portland" --
                    # genuinely ambiguous (Portland, ME vs Portland, OR).
                    sub_street, sub_city, sub_state = _split_address(
                        f"{parcel_address}, Maine"
                    )
                    rows.append({
                        "id": sub_id,
                        "url": urljoin(self.base_url, sub_href),
                        # Date/status inherited from the portfolio listing
                        # (individual parcel pages get their own chance to
                        # refine date_time via the normal parse_detail()
                        # path below, same as any other listing -- this is
                        # just the starting value in case that page's own
                        # date doesn't parse for some reason).
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

        # Find the actual header line rather than assuming lines[0] --
        # confirmed necessary: the Auction 26-63 portfolio index page
        # (i=5641) has several preamble lines first ("Property
        # Information Package By City Available", disclosure/lead-paint
        # notices, etc.) before its "Real Estate Foreclosure Auction..."
        # line, which would silently misalign a fixed lines[0..4] read.
        # That specific page gets expanded/skipped in parse_listing()
        # before ever reaching here, but a normal single-property page
        # HAVING similar preamble text hasn't been ruled out either way,
        # so this searches rather than assumes even for the common case.
        header_idx = next(
            (i for i, ln in enumerate(lines) if _HEADER_RE.match(ln)), None
        )
        if header_idx is None or len(lines) < header_idx + 5:
            # No recognizable header at all -- bail out rather than guess
            # at a different layout. The row's `url` (always set from
            # parse_listing()) already points at the original page, so a
            # human can still read the real listing directly -- no need
            # to duplicate that content into the row itself.
            return {}
        lines = lines[header_idx:]

        result = {}

        # Line 4 has a full date WITH year, unlike the list page -- prefer
        # it as authoritative when it parses. Same "detail refines list"
        # pattern as patriot.py's parse_detail().
        try:
            detail_dt = date_parser.parse(lines[4], fuzzy=True)
            result["date_time"] = lines[4]
            result["auction_dt"] = detail_dt
            result["timing"] = classify_timing(detail_dt)
        except (ValueError, OverflowError):
            pass

        labeled = _extract_labeled_lines(lines[5:])
        description = labeled.get("Real Estate", "")
        # Deliberately NOT setting result["description"] here. The row
        # already carries a short title-like phrase from parse_listing()
        # (e.g. "2BR Cape Style Home - 1+/- Acres", "31-Unit Apartment
        # Building") -- genuinely useful as a glanceable one-line summary
        # without clicking through to `url`. Overwriting it with this
        # full "Real Estate:" paragraph (a whole paragraph, not a
        # summary) would trade a useful short label for a long one, and
        # only for listings that happen to have this specific section --
        # commercial/land/portfolio listings never had a "Real Estate:"
        # label at all, so they'd keep the short version while plain
        # residential ones lost it. `description` (local var) still
        # feeds the AI call below either way.

        # AI extraction, always on -- see module docstring for why this
        # site doesn't get a no-AI mode the way sullivan/jjmanning/patriot
        # do. Failure here (missing ANTHROPIC_API_KEY, API error, etc.)
        # degrades to blank spec fields rather than breaking the run --
        # the rest of this row (address/date/status/description/terms)
        # already came from parse_listing()/the lines above and is
        # unaffected.
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
            ai_extra = specs.extra_fields
        else:
            ai_extra = None

        extra_parts = []
        for label in ("Preview", "Directions", "Terms"):
            if label in labeled:
                extra_parts.append(f"{label}: {labeled[label]}")
        if ai_extra:
            extra_parts.append(ai_extra)

        if extra_parts:
            result["extra_fields"] = "; ".join(extra_parts)

        # First real use of the metadata_json flex field (see schema.sql):
        # a structured, source-specific link that doesn't fit any
        # existing column and isn't free text either -- exactly what
        # that field was reserved for, rather than folding it into
        # extra_fields as one more semicolon-joined string.
        pip_link = soup.find("a", href=re.compile(r"getinfo\.cgi", re.IGNORECASE))
        if pip_link and pip_link.get("href"):
            result["metadata"] = json.dumps(
                {"pip_link": urljoin(self.base_url, pip_link["href"])}
            )

        pdf_links = [
            clean_url(urljoin(self.base_url, a["href"]))
            for a in soup.find_all("a", href=True)
            if a["href"].lower().endswith(".pdf")
        ]
        if pdf_links:
            result["pdf_links"] = "; ".join(pdf_links)

        return result