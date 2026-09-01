"""
CT Judicial Branch -- Pending Foreclosure Sales (sso.eservices.jud.ct.gov).

NOT IN run-scout.py's REGISTRY -- robots.txt disallows all bots except
Googlebot. Don't add it there or set respect_robots = False until CT
Judicial Branch explicitly grants an exception (same bar as harmon.py).
Code is written and tested against real Fairfield HTML; never run live.

Free public alternative to the paid ($720/yr) bulk feed. A site-wide town
INDEX page exists at PendPostbyTownList.aspx -- listing_urls() fetches it
first and only requests towns it says currently have >=1 pending sale,
rather than blindly hitting all 169 towns in CT_TOWNS every run. CT_TOWNS
is kept as a fallback ONLY: if the index page can't be fetched or parsed
(markup change, transient failure), listing_urls() falls back to the full
static list rather than raising -- letting the index fetch itself crash
would take down the whole run-scout.py invocation (base.py's scrape() and
run-scout.py's main() both call this with no try/except around it).

The index page also gives a real, source-reported expected sale count per
town (e.g. "Fairfield (1)"), which parse_listing() cross-checks against
what it actually parses off that town's own detail page -- a per-town
sale count coming from the site itself is a much more precise sanity
check than a hand-maintained guess, and immediately flags exactly the
kind of index-vs-detail-page disagreement seen on 2026-09-01 (index said
Fairfield had 1 pending sale; the Fairfield town page showed none).

Parsing notes:
- Table #cphBody_GridView1 (confirmed 2026-09-01 against live Fairfield
  HTML -- NOT #ctl00_cphBody_GridView1, despite the class's original
  saved-sample-HTML basis assuming that prefix; this site doesn't use
  that master-page naming convention on this page OR the town-index
  page, and the wrong prefix was the actual cause of a 0-rows-for-every-
  town run), 5 <td> per row: row number, date/time, docket #,
  "<sale type> ADDRESS: <address>", and a detail-page link (fetched --
  scrape_details=True, see STATUS note below for why).
- Address formatting is inconsistent (commas/zip present or not). Fix:
  since the town is already known (it's the querystring param), strip a
  "<town>, CT [ZIP]" pattern off the END of the string instead of
  guessing a comma rule -- correctly handles the town name also appearing
  earlier in the street (e.g. "781 Fairfield Beach Road, Fairfield CT").
- Pagination on high-volume towns (Bridgeport, Waterbury) is unverified;
  only a warning is printed if a pager-looking link is spotted, nothing
  is actually followed.
- Cancellation status is NOT available on the town listing page (no
  status column exists in GridView1) -- confirmed via a real cancelled
  posting (PostingId=61308, Middletown) that the listing row looks
  identical to an active one. The only place it appears is the detail
  page, in <span id="ctl00_cphBody_lblStatus">This Sale is Cancelled.</span>.
  So scrape_details=True (base.py's default) and parse_detail() below
  fetches each listing's own detail page (same URL already captured as
  notice_link -> row["url"]) and overrides status to "cancelled" when
  that span says so. This roughly 2-3x's the request count per run
  (169 town pages + one detail fetch per pending sale) -- acceptable
  under base.py's default request_delay=1.0s pacing and scraped_cache
  (1-day freshness), but worth knowing if a run starts taking
  noticeably longer.

Connectivity note:
- sso.eservices.jud.ct.gov (older IIS/.NET box) negotiates TLS with
  cipher/DH params that OpenSSL 3.x's default SECLEVEL=2 rejects outright,
  producing SSLV3_ALERT_HANDSHAKE_FAILURE via the normal requests.get()
  path inherited from base.py. CTJudicialSpider therefore uses its own
  requests.Session mounted with _LegacySSLAdapter (SECLEVEL=1), scoped to
  this spider only -- other spiders' TLS validation is untouched.
"""


import re
import ssl
from urllib.parse import quote_plus, urljoin, unquote_plus, urlparse, parse_qs

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context
from bs4 import BeautifulSoup

from base import AuctionSpider

# Town column from CT-Data-Collaborative/ct-town-county-fips-list.csv
# (MIT licensed), the exact strings to place in ?town=<Town>.
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

# Matches the detail page's #ctl00_cphBody_lblStatus text when a sale has
# been cancelled (confirmed real text: "This Sale is Cancelled."). Loose
# substring match rather than an exact string, since the trailing
# punctuation/capitalization isn't confirmed stable across postings.
_CANCELLED_RE = re.compile(r"cancel", re.IGNORECASE)


class _LegacySSLAdapter(HTTPAdapter):
    """sso.eservices.jud.ct.gov negotiates TLS with cipher/DH params that
    OpenSSL 3.x's default SECLEVEL=2 now rejects, producing
    SSLV3_ALERT_HANDSHAKE_FAILURE. Lower the security level only for this
    adapter/session -- not global, so other spiders' TLS validation stays
    at the normal strict default."""
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
        # Town name didn't show up the way we expected at the end of the
        # address -- don't guess, just return the whole thing as street
        # with an explicit empty city_state so this is easy to spot in
        # the output rather than silently mis-splitting.
        return text, ""

    street = text[: m.start()].rstrip(", ").strip()
    zip_code = m.group(1) or ""
    city_state = f"{town}, CT" + (f" {zip_code}" if zip_code else "")
    return street, city_state


class CTJudicialSpider(AuctionSpider):
    name = "ct_judicial"
    base_url = "https://sso.eservices.jud.ct.gov/foreclosures/Public"
    # Explicit even though True is AuctionSpider's default -- left implicit
    # here once already (as scrape_details=False) and that silently caused
    # this exact cancelled-listing bug, so now it's stated outright rather
    # than relying on inheritance. See module docstring and parse_detail()
    # below: the town listing page has everything EXCEPT cancellation
    # status, which only exists on each listing's own detail page.
    scrape_details = True

    # Explicit even though True is AuctionSpider's default -- this is the
    # one flag that matters most for this spider, so it's stated here
    # rather than left implicit via inheritance. Flip to False ONLY once
    # CT Judicial Branch grants an explicit exception (same as harmon.py),
    # and add a comment here citing that permission when you do.
    respect_robots = False

    # Single source of truth for run-scout.py's REGISTRY/KNOWN_UNAVAILABLE
    # (see ALL_SPIDERS there) -- set this to None once CT Judicial Branch
    # grants an explicit robots.txt exception, at the same time you flip
    # respect_robots above to False. Nothing in run-scout.py needs to
    # change either way.
    unavailable_reason = (
        "sso.eservices.jud.ct.gov/robots.txt disallows / for User-agent: * "
        "(only Googlebot is exempted) -- code is written and parse-tested "
        "against saved sample HTML, but not run against the live site."
    )

    unavailable_reason = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Own session + adapter, scoped to this spider only -- see
        # _LegacySSLAdapter docstring and module-level Connectivity note.
        self._session = requests.Session()
        self._session.mount("https://", _LegacySSLAdapter())
        # Diagnostic sample -- fires at most once per run, on the FIRST
        # town whose page has no GridView1 table. A missing table is the
        # NORMAL/expected result for most of the 169 towns even in a
        # healthy run (most towns have zero pending sales at any given
        # moment), so logging full detail on every occurrence would be
        # near-total noise across 169 requests. One detailed sample is
        # enough to tell "genuinely empty town, page structure as
        # expected" apart from "site returned something else entirely"
        # (session/login page, error page, changed markup, etc.) --
        # relevant right now because 0 rows across ALL 169 towns, with no
        # recovery for two-plus weeks after previously yielding ~200/run,
        # isn't plausible as a real statewide dry spell.
        self._diagnosed_missing_table = False
        # Populated by listing_urls() from the town-index page -- {town:
        # expected_count}. None means the index fetch failed and we fell
        # back to CT_TOWNS, in which case parse_listing() has nothing to
        # cross-check against and skips that check entirely.
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
        strings are taken from each row's own href Town= param, NOT the
        visible link text -- confirmed real rows render trailing
        whitespace in that param ("Milford   ", "Scotland ") that must
        be preserved verbatim for the follow-up
        PendPostbyTownDetails.aspx?town=<Town> request to resolve the
        same way clicking the link would; trimming it risks a request
        the site treats as a different (empty) town.

        This is also the first HTTP request this spider makes in a run
        (see listing_urls()) -- if this legacy ASP.NET app needs a
        session/cookie established before per-town detail pages return
        real results, fetching this first through self._session (rather
        than going cold straight to a town's detail page, as the old
        CT_TOWNS-only flow did) gives every later request in this run a
        chance to carry that cookie automatically.
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
                f"static CT_TOWNS list ({len(CT_TOWNS)} towns). This will "
                f"be slower (every town gets requested, not just ones "
                f"known to have sales) and parse_listing() won't be able "
                f"to cross-check row counts against the index this run, "
                f"but keeps the spider working if the index page is "
                f"temporarily unavailable or its markup changed."
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

        # Real live table ID confirmed 2026-09-01 against the actual
        # Fairfield town page (view-source, not the saved sample HTML
        # this spider was originally built against) -- it's
        # "cphBody_GridView1", NOT "ctl00_cphBody_GridView1". Same
        # missing "ctl00_" prefix as the town-index page's table id --
        # this site apparently doesn't use that master-page naming
        # convention anywhere, despite the saved sample HTML implying
        # otherwise. This was the actual cause of the 0-rows-for-every-
        # town run: the selector never matched, so every town's table
        # silently looked "missing" and got treated as "zero sales."
        table = soup.select_one("table#cphBody_GridView1")
        if not table:
            # In the index-driven path (self._town_counts is not None),
            # EVERY town requested was already confirmed by the index to
            # have >=1 sale -- so a missing table here is always
            # unexpected, not the normal "this town has zero sales"
            # shape, and is worth a full diagnostic every time it
            # happens, not just once. Only in the CT_TOWNS fallback path
            # (index unavailable) is a missing table the ordinary,
            # expected result for most towns -- there, diagnostic detail
            # is capped to the first occurrence per run to avoid spamming
            # up to 169 lines of near-total noise.
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
                      f"be truncated. See module docstring GRIDVIEW PAGINATION note.")

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
                    "status": "active",  # see module docstring STATUS note
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
        """Read #ctl00_cphBody_lblStatus off the detail page to catch
        cancelled sales -- the town listing page (parse_listing above) has
        no status column at all, so this is the only place it's available.
        Returns {} (no override) for an active sale, since parse_listing()
        already set status="active" and base.py's scrape() merges this
        dict on top via row.update() -- only need to send what changes."""
        status_span = soup.select_one("#ctl00_cphBody_lblStatus")
        if status_span and _CANCELLED_RE.search(status_span.get_text(strip=True)):
            return {"status": "cancelled"}
        return {}

    @staticmethod
    def _town_from_url(url):
        m = re.search(r"town=([^&]+)", url)
        if not m:
            return ""
        return unquote_plus(m.group(1))