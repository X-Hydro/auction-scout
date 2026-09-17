"""
CT Judicial Branch -- Pending Foreclosure Sales (sso.eservices.jud.ct.gov).

robots.txt disallows all bots except Googlebot, so this is not run live --
written and parse-tested against saved sample HTML only. Free alternative
to the paid ($720/yr) bulk feed.

A site-wide town INDEX page (PendPostbyTownList.aspx) lists which towns
currently have >=1 pending sale, plus a per-town expected count.
listing_urls() uses it to only request towns with sales instead of all
169 in CT_TOWNS, and parse_listing() cross-checks its own row count
against the index's expected count per town. CT_TOWNS is the fallback if
the index can't be fetched/parsed.

Parsing notes:
- Table #cphBody_GridView1 (no "ctl00_" prefix -- this site doesn't use
  that master-page naming convention), 5 <td> per row: row number,
  date/time, docket #, "<sale type> ADDRESS: <address>", and a detail-
  page link.
- Address formatting is inconsistent (commas/zip present or not); since
  the town is already known, a "<town>, CT [ZIP]" pattern is stripped
  off the END of the string rather than guessing a comma rule, so a town
  name that also appears earlier in the street doesn't cause a bad split.
- Pagination on high-volume towns is unverified -- only a warning is
  printed if a pager-looking link is spotted, nothing is followed.
- Cancellation status only exists on each listing's own detail page --
  confirmed rendered as <span id="cphBody_lblStatus">This Sale is
  Cancelled.</span>, but parse_detail() searches the whole page text for
  "cancel" rather than that specific element, so a future id/markup
  change doesn't silently break detection again. The town listing page
  has no status column at all. Hence scrape_details=True and
  parse_detail() below.

Connectivity note:
- sso.eservices.jud.ct.gov negotiates TLS with cipher/DH params that
  OpenSSL 3.x's default SECLEVEL=2 rejects (SSLV3_ALERT_HANDSHAKE_FAILURE).
  CTJudicialSpider uses its own requests.Session with _LegacySSLAdapter
  (SECLEVEL=1), scoped to this spider only.
"""


import re
import ssl
from urllib.parse import quote_plus, urljoin, unquote_plus, urlparse, parse_qs

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context
from bs4 import BeautifulSoup

from base import AuctionSpider

# Exact strings to place in ?town=<Town>.
CT_TOWNS = [
    "Andover", "Ansonia", "Ashford", "Avon", "Barkhamsted", "Beacon Falls",
    "Berlin", "Bethany", "Bethel", "Bethlehem", "Bloomfield", "Bolton",
    "Bozrah", "Branford", "Bridgeport", "Bridgewater", "Bristol",
    "Brookfield", "Brooklyn", "Burlington", "Canaan", "Canterbury",
    "Canton", "Chaplin", "Cheshire", "Chester", "Clinton", "Colchester",
    "Colebrook", "Columbia", "Cornwall", "Coventry", "Cromwell",
    "Danbury", "Darien", "Deep River", "Derby", "Durham", "East Granby",
    "East Haddam", "East Hampton", "East Hartford", "East Haven",
    "East Lyme", "East Windsor", "Eastford", "Easton", "Ellington",
    "Enfield", "Essex", "Fairfield", "Farmington", "Franklin",
    "Glastonbury", "Goshen", "Granby", "Greenwich", "Griswold",
    "Groton", "Guilford", "Haddam", "Hamden", "Hampton", "Hartford",
    "Hartland", "Harwinton", "Hebron", "Kent", "Killingly", "Killingworth",
    "Lebanon", "Ledyard", "Lisbon", "Litchfield", "Lyme", "Madison",
    "Manchester", "Mansfield", "Marlborough", "Meriden", "Middlebury",
    "Middlefield", "Middletown", "Milford", "Monroe", "Montville",
    "Morris", "Naugatuck", "New Britain", "New Canaan", "New Fairfield",
    "New Hartford", "New Haven", "New London", "New Milford", "Newington",
    "Newtown", "Norfolk", "North Branford", "North Canaan",
    "North Haven", "North Stonington", "Norwalk", "Norwich", "Old Lyme",
    "Old Saybrook", "Orange", "Oxford", "Plainfield", "Plainville",
    "Plymouth", "Pomfret", "Portland", "Preston", "Prospect", "Putnam",
    "Redding", "Ridgefield", "Rocky Hill", "Roxbury", "Salem",
    "Salisbury", "Scotland", "Seymour", "Sharon", "Shelton", "Sherman",
    "Simsbury", "Somers", "South Windsor", "Southbury", "Southington",
    "Sprague", "Stafford", "Stamford", "Sterling", "Stonington",
    "Stratford", "Suffield", "Thomaston", "Thompson", "Tolland",
    "Torrington", "Trumbull", "Union", "Vernon", "Voluntown", "Wallingford",
    "Warren", "Washington", "Waterbury", "Waterford", "Watertown",
    "West Hartford", "West Haven", "Westbrook", "Weston", "Westport",
    "Wethersfield", "Willington", "Wilton", "Winchester", "Windham",
    "Windsor", "Windsor Locks", "Wolcott", "Woodbridge", "Woodbury",
    "Woodstock",
]

#CT_TOWNS = ["Fairfield"]

# "12:00PM" -> "12:00 PM" (no space between minutes and AM/PM in the source)
_TIME_SPACE_RE = re.compile(r"(\d{2})\s*([AP]M)\b", re.IGNORECASE)

_PAGER_RE = re.compile(r"GridView1.*Page\$", re.IGNORECASE)

# Loose substring match rather than an exact string, since the trailing
# punctuation/capitalization of "This Sale is Cancelled." isn't confirmed
# stable across postings.
_CANCELLED_RE = re.compile(r"cancel", re.IGNORECASE)

# Some rows embed a case-status note directly after the address in the
# same GridView cell (e.g. "...CT CANCELLED CANCELLED - JUDGMENT OPENED
# AND VACATED,"), which breaks _split_address()'s end-anchored town
# match. Not part of the address -- strip it before splitting.
_TRAILING_NOTE_RE = re.compile(r"\s*CANCELLED\b.*$", re.IGNORECASE)


class _LegacySSLAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


def _normalize_datetime(raw):
    """'08/15/2026 12:00PM' -> '08/15/2026 12:00 PM'."""
    if not raw:
        return ""
    return _TIME_SPACE_RE.sub(r"\1 \2", raw).strip()


def _split_address(text, town):
    """Split '<street> ... <town>, CT [ZIP]' into (street, city_state),
    anchoring on the known town at the END of the string so a town name
    that also appears earlier (in the street) doesn't cause a bad split."""
    text = text.strip()
    if not text:
        return "", ""

    tail_re = re.compile(
        r",?\s*" + re.escape(town) + r"\s*,?\s*CT\.?\s*(\d{5})?\s*$",
        re.IGNORECASE,
        )
    m = tail_re.search(text)
    if not m:
        # Doesn't match the expected shape -- return the whole thing as
        # street with an empty city_state rather than guessing.
        return text, ""

    street = text[: m.start()].rstrip(", ").strip()
    zip_code = m.group(1) or ""
    city_state = f"{town}, CT" + (f" {zip_code}" if zip_code else "")
    return street, city_state


class CTJudicialSpider(AuctionSpider):
    name = "ct_judicial"
    base_url = "https://sso.eservices.jud.ct.gov/foreclosures/Public"
    scrape_details = True
    respect_robots = False

    # Single source of truth for run-scout.py's registry. Set to None once
    # CT Judicial Branch grants an explicit robots.txt exception.
    unavailable_reason = (
        "sso.eservices.jud.ct.gov/robots.txt disallows / for User-agent: * "
        "(only Googlebot is exempted) -- code is written and parse-tested "
        "against saved sample HTML, but not run against the live site."
    )

    unavailable_reason = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._session = requests.Session()
        self._session.mount("https://", _LegacySSLAdapter())
        # A missing GridView1 table is the normal/expected result for most
        # towns (no pending sales), so this only logs full diagnostic
        # detail on the first occurrence per run rather than up to 169
        # times -- enough to tell "genuinely empty town" apart from "site
        # returned something else entirely."
        self._diagnosed_missing_table = False
        # {town: expected_count} from the town-index page; None if that
        # fetch failed and parse_listing() has nothing to cross-check.
        self._town_counts = None

    def get_soup(self, url):
        resp = self._session.get(
            url, headers={"User-Agent": self.user_agent}, timeout=20
        )
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")

    def _fetch_town_counts(self):
        """Fetch PendPostbyTownList.aspx and return {town: count} for
        towns the index says currently have >=1 pending sale. Town
        strings come from each row's href Town= param, not the visible
        link text -- some render trailing whitespace that must be kept
        verbatim for the follow-up PendPostbyTownDetails.aspx?town=<Town>
        request to resolve the same town.
        """
        index_url = f"{self.base_url}/PendPostbyTownList.aspx"
        soup = self.get_soup(index_url)
        table = soup.select_one("table#cphBody_GridView1")
        if not table:
            raise ValueError(
                f"{index_url}: expected table#cphBody_GridView1 not found "
                f"-- index page markup may have changed"
            )

        counts = {}
        for tr in table.find_all("tr"):
            link = tr.find("a", href=True)
            span = tr.find("span")
            if not link or not span:
                continue  # header row (th, no matching a/span pair)
            qs = parse_qs(urlparse(link["href"]).query)
            town = qs.get("Town", [None])[0]
            if not town:
                continue
            try:
                count = int(span.get_text(strip=True))
            except ValueError:
                continue
            if count > 0:
                counts[town] = count
        return counts

    def listing_urls(self):
        try:
            self._town_counts = self._fetch_town_counts()
        except Exception as e:
            print(
                f"[{self.name}] WARNING: could not read the town index "
                f"({type(e).__name__}: {e}) -- falling back to the full "
                f"static CT_TOWNS list ({len(CT_TOWNS)} towns)."
            )
            self._town_counts = None
            towns = CT_TOWNS
        else:
            towns = list(self._town_counts)
            total_sales = sum(self._town_counts.values())
            print(
                f"[{self.name}] Town index: {len(towns)}/{len(CT_TOWNS)} "
                f"town(s) currently show a pending sale, {total_sales} "
                f"sale(s) total -- only fetching those towns."
            )

        return [
            f"{self.base_url}/PendPostbyTownDetails.aspx?town={quote_plus(town)}"
            for town in towns
        ]

    def parse_listing(self, soup, listing_url):
        town = self._town_from_url(listing_url)
        expected = self._town_counts.get(town) if self._town_counts is not None else None

        table = soup.select_one("table#cphBody_GridView1")
        if not table:
            # In the index-driven path, every town requested was already
            # confirmed to have a sale, so a missing table here is always
            # unexpected. In the CT_TOWNS fallback path it's the ordinary
            # result for most towns, so detail is capped to one occurrence.
            if expected is not None or not self._diagnosed_missing_table:
                self._diagnosed_missing_table = True
                title = soup.title.get_text(strip=True) if soup.title else "(no <title>)"
                other_tables = len(soup.find_all("table"))
                has_viewstate = soup.select_one("input#__VIEWSTATE") is not None
                page_len = len(str(soup))
                print(
                    f"[{self.name}] DIAGNOSTIC ({town}, index expected={expected}): "
                    f"no table#cphBody_GridView1 found -- title={title!r}, "
                    f"other <table> elements on page={other_tables}, "
                    f"has __VIEWSTATE={has_viewstate}, page length={page_len} chars -- "
                    f"a normal page should still look like a real ASP.NET "
                    f"postback page (has __VIEWSTATE, familiar title); if this looks "
                    f"different, the site is likely returning a session/error/interstitial "
                    f"page instead of real results."
                )
            rows = []
        else:
            if any(_PAGER_RE.search(a.get("href", "")) for a in table.find_all("a", href=True)):
                print(f"[{self.name}] {town}: looks like GridView1 has a pager control -- "
                      f"this spider does NOT follow pagination, results for this town may "
                      f"be truncated.")

            rows = []
            for tr in table.find_all("tr"):
                cells = tr.find_all("td")
                if len(cells) < 5:
                    continue  # header row (all <th>) or a stray non-data row

                date_cell, docket_cell, desc_cell, notice_cell = (
                    cells[1], cells[2], cells[3], cells[4],
                )

                date_time = _normalize_datetime(date_cell.get_text(" ", strip=True))

                docket_link = docket_cell.find("a")
                docket_no = docket_link.get_text(strip=True) if docket_link else ""
                if not docket_no:
                    print(f"[{self.name}] {town}: row with no docket number -- skipping")
                    continue

                desc_text = desc_cell.get_text(" ", strip=True)
                desc_text = re.sub(r"\s+", " ", desc_text)
                m = re.search(r"ADDRESS\s*:\s*(.*)$", desc_text, re.IGNORECASE)
                if m:
                    sale_type = desc_text[: m.start()].strip().rstrip(":").strip()
                    address_raw = m.group(1).strip()
                    address_raw = _TRAILING_NOTE_RE.sub("", address_raw).strip()
                else:
                    sale_type, address_raw = desc_text, ""

                street, city_state = _split_address(address_raw, town)

                notice_link = notice_cell.find("a")
                url = (
                    urljoin(f"{self.base_url}/", notice_link["href"])
                    if notice_link and notice_link.get("href")
                    else listing_url
                )

                rows.append({
                    "id": docket_no,
                    "url": url,
                    "date_time": date_time,
                    "status": "active",  # cancellation is only knowable via parse_detail()
                    "street": street,
                    "city_state": city_state,
                    "county": "",  # not exposed on this page
                    "description": sale_type,
                    "extra_fields": f"Town: {town}",
                    "pdf_links": "",
                })

        if expected is not None and len(rows) != expected:
            print(
                f"[{self.name}] WARNING: {town}: town index said "
                f"{expected} pending sale(s), but parsed {len(rows)} "
                f"from this town's own page -- either a row failed to "
                f"parse (e.g. missing docket number, see warnings "
                f"above), the table was missing entirely (see DIAGNOSTIC "
                f"above), or the site's index and detail pages disagree "
                f"with each other."
            )

        return rows

    def parse_detail(self, soup, row):
        """Cancellation only appears on the detail page -- searched across
        the whole page text (not tied to one specific element) so a future
        markup/id change can't silently break this the way the missing
        "ctl00_" prefix did. Returns {} (no override) for an active sale,
        since parse_listing() already set status="active" and base.py
        merges this dict on top."""
        if _CANCELLED_RE.search(soup.get_text(" ", strip=True)):
            return {"status": "cancelled"}
        return {}

    @staticmethod
    def _town_from_url(url):
        m = re.search(r"town=([^&]+)", url)
        if not m:
            return ""
        return unquote_plus(m.group(1))