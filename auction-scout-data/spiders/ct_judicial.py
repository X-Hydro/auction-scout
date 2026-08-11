"""
CT Judicial Branch -- Pending Foreclosure Sales (sso.eservices.jud.ct.gov).

NOT IN run-scout.py's REGISTRY -- robots.txt disallows all bots except
Googlebot. Don't add it there or set respect_robots = False until CT
Judicial Branch explicitly grants an exception (same bar as harmon.py).
Code is written and tested against real Fairfield HTML; never run live.

Free public alternative to the paid ($720/yr) bulk feed. No site-wide
listing page exists, so CT_TOWNS (all 169 towns) gets enumerated one
?town=<Name> request at a time against PendPostbyTownDetails.aspx.

Parsing notes:
- Table #ctl00_cphBody_GridView1, 5 <td> per row: date/time, docket #,
  "<sale type> ADDRESS: <address>", and a detail-page link (not fetched --
  scrape_details=False, this page already has everything we need).
- Address formatting is inconsistent (commas/zip present or not). Fix:
  since the town is already known (it's the querystring param), strip a
  "<town>, CT [ZIP]" pattern off the END of the string instead of
  guessing a comma rule -- correctly handles the town name also appearing
  earlier in the street (e.g. "781 Fairfield Beach Road, Fairfield CT").
- No sample of a postponed/canceled listing exists -- status is hardcoded
  "active".
- Pagination on high-volume towns (Bridgeport, Waterbury) is unverified;
  only a warning is printed if a pager-looking link is spotted, nothing
  is actually followed.
"""


import re
from urllib.parse import quote_plus, urljoin

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

# "12:00PM" -> "12:00 PM" (no space between minutes and AM/PM in the source)
_TIME_SPACE_RE = re.compile(r"(\d{2})\s*([AP]M)\b", re.IGNORECASE)

_PAGER_RE = re.compile(r"GridView1.*Page\$", re.IGNORECASE)


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
    scrape_details = False  # town listing page already has everything we need

    # Explicit even though True is AuctionSpider's default -- this is the
    # one flag that matters most for this spider, so it's stated here
    # rather than left implicit via inheritance. Flip to False ONLY once
    # CT Judicial Branch grants an explicit exception (same as harmon.py),
    # and add a comment here citing that permission when you do.
    respect_robots = True

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

    def listing_urls(self):
        return [
            f"{self.base_url}/PendPostbyTownDetails.aspx?town={quote_plus(town)}"
            for town in CT_TOWNS
        ]

    def parse_listing(self, soup, listing_url):
        town = self._town_from_url(listing_url)

        table = soup.select_one("table#ctl00_cphBody_GridView1")
        if not table:
            return []  # town with zero pending sales -- table doesn't render at all

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

        return rows

    @staticmethod
    def _town_from_url(url):
        m = re.search(r"town=([^&]+)", url)
        if not m:
            return ""
        from urllib.parse import unquote_plus
        return unquote_plus(m.group(1))