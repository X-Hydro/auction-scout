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
"""

import re

from base import AuctionSpider, DATE_CHUNK_RE

HARMON_ID_START = 1899
HARMON_ID_END = 1942


class HarmonSpider(AuctionSpider):
    name = "harmon"
    base_url = "https://www.harmonlawoffices.com"
    scrape_details = False  # each "listing" IS the detail page, no second fetch needed
    respect_robots = False  # see module docstring -- direct permission granted

    def listing_urls(self):
        return [
            f"{self.base_url}/auction/{i}"
            for i in range(HARMON_ID_START, HARMON_ID_END + 1)
        ]

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
        timetitle = self._text(block.select_one(".timetitle"))
        timedescription = self._text(block.select_one(".timedescription"))
        registry = self._text(block.select_one(".registry"))

        # timedescription is EITHER a real date/time OR a status word
        # (CANCELLED, POSTPONED, SOLD, etc.) -- reuse base.py's date-chunk
        # regex to tell which one we're looking at.
        if DATE_CHUNK_RE.search(timedescription):
            date_time = timedescription
            status = "active"
        else:
            date_time = ""
            status = timedescription.strip().lower() or "unknown"

        description_parts = [p for p in [auction_type, timetitle, registry] if p]

        return [{
            "id": str(auction_id),
            "url": listing_url,
            "date_time": date_time,
            "status": status,
            "street": street,
            "city_state": city_state,
            "description": " | ".join(description_parts),
            "extra_fields": f"Deposit: {deposit}" if deposit else "",
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