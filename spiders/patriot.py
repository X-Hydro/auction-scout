"""
Patriot Auctioneers (patriotauctioneers.com) -- "A Sullivan Company".

Shares backend infrastructure with sullivan-auctioneers.com (image/PDF URLs
literally point at sullivan-auctioneers.com/admin/uploader and
sullivan-auctioneers.com/public_docs), but is a SEPARATE site with its own
listings -- not covered by spiders/sullivan.py.

CALENDAR PAGE (discovery + status filtering):
Each listing is a self-contained <a class="auction-list" href="/calendar-detail/?id=N">
containing an <h1>ADDRESS - CITY, ST</h1>, an .auction-date div, and an
optional .banner-wrapper > .banner (class "red" = cancelled, class "yellow"
= postponed, ABSENT = current/active). This banner class is used for status
detection -- not text matching -- same robustness principle as jjmanning.py's
date-icon-box-presence check.

POSTPONED/DEDUP: same id is reused across a postponement (confirmed: id=21004
appears both as the stale "postponed" row at the old date AND the current
row at the new date with no banner). Priority-merge dedup here, same pattern
as spiders/towne.py, but keyed on the site's own native id (like Sullivan)
rather than an address-derived composite (Towne had no native id).

CANCELLED: confirmed these do NOT reappear elsewhere in the list (checked
manually against 4 examples) -- genuinely dead, not just stale. Skipped
entirely per explicit instruction.

DATES HAVE NO YEAR ("Tuesday Jul 7", "Postponed until Friday Aug 7 @ 2:00 pm").
Parsed with dateutil fuzzy + a manual year-rollover correction (see
_parse_no_year_date) so this doesn't silently misparse dates near a year
boundary (e.g. a January date scraped in December).

DETAIL PAGE: structured in labeled ".auction-box" sections (Property Details,
Auction Date, Auction Location, Terms of Sale, Documents) -- close cousin of
Sullivan's own detail page layout. Auction Location's address is used
preferentially over the calendar page's since it includes the ZIP code.
"""

import re
from datetime import datetime, timedelta
from urllib.parse import urljoin

from dateutil import parser as date_parser

from base import AuctionSpider, classify_timing, clean_url

STATUS_PRIORITY = {"active": 2, "postponed": 1}


def _parse_no_year_date(text, reference=None):
    """Dates on this site never include a year. Parse assuming 'reference'
    year (default: now), then roll forward a year if that lands the date
    more than 30 days in the past -- handles auctions scraped near a
    year boundary (e.g. a January date scraped in December)."""
    reference = reference or datetime.now()
    text = text.strip()
    if not text:
        return None
    try:
        dt = date_parser.parse(text, fuzzy=True, default=reference)
    except (ValueError, OverflowError):
        return None
    if dt < reference - timedelta(days=30):
        dt = dt.replace(year=dt.year + 1)
    return dt


def _clean_text(text):
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _find_box(soup, header_substr):
    """Find an .auction-box div on the detail page by its <h3> text."""
    for box in soup.select(".auction-box"):
        h3 = box.find("h3")
        if h3 and header_substr.lower() in h3.get_text(strip=True).lower():
            return box
    return None


class PatriotSpider(AuctionSpider):
    name = "patriot"
    base_url = "https://patriotauctioneers.com"
    scrape_details = True  # detail page has the ZIP-precise address + terms

    def listing_urls(self):
        return [f"{self.base_url}/calendar/"]

    def parse_listing(self, soup, listing_url):
        rows_by_id = {}

        for a in soup.select("a.auction-list"):
            href = a.get("href", "")
            m = re.search(r"id=(\d+)", href)
            auction_id = m.group(1) if m else None
            if not auction_id:
                continue

            banner = a.select_one(".banner-wrapper .banner")
            banner_classes = banner.get("class", []) if banner else []
            if "red" in banner_classes:
                continue  # cancelled -- confirmed these don't reappear, skip outright

            status_key = "postponed" if "yellow" in banner_classes else "active"

            date_div = a.select_one(".auction-date")
            primary_date_text = ""
            if date_div:
                first_string = date_div.find(string=True, recursive=False)
                primary_date_text = first_string.strip() if first_string else ""

            postponed_span = date_div.select_one("span.text-yellow") if date_div else None
            postponed_text = postponed_span.get_text(strip=True) if postponed_span else ""

            if status_key == "postponed" and postponed_text:
                m2 = re.search(r"Postponed until (.+)", postponed_text, re.IGNORECASE)
                effective_date_text = m2.group(1).strip() if m2 else postponed_text
            else:
                effective_date_text = primary_date_text

            auction_dt = _parse_no_year_date(effective_date_text)
            timing = classify_timing(auction_dt) if auction_dt else "Unknown"

            h1 = a.select_one("h1")
            title_text = h1.get_text(strip=True) if h1 else ""
            if " - " in title_text:
                street, city_state = title_text.split(" - ", 1)
            else:
                street, city_state = title_text, ""

            desc_divs = a.select(".auction-short-desc")
            prop_type = desc_divs[0].get_text(strip=True) if desc_divs else ""

            row = {
                "id": auction_id,
                "url": urljoin(self.base_url, href),
                "date_time": effective_date_text,
                "auction_dt": auction_dt,
                "timing": timing,
                "status": status_key,
                "street": street.strip(),
                "city_state": city_state.strip(),
                "description": prop_type,
                "extra_fields": "",
                "pdf_links": "",
            }

            existing = rows_by_id.get(auction_id)
            if existing is None:
                rows_by_id[auction_id] = row
            elif STATUS_PRIORITY.get(status_key, 0) > STATUS_PRIORITY.get(existing["status"], 0):
                rows_by_id[auction_id] = row

        return list(rows_by_id.values())

    def parse_detail(self, soup, row):
        result = {}

        location_box = _find_box(soup, "Auction Location")
        addr_strong = location_box.select_one("p strong") if location_box else None
        if addr_strong:
            for br in addr_strong.find_all("br"):
                br.replace_with(" || ")
            addr_text = addr_strong.get_text(strip=True)
            parts = [p.strip() for p in addr_text.split("||")]
            if len(parts) >= 2:
                result["street"] = parts[0]
                result["city_state"] = parts[1]

        details_box = _find_box(soup, "Property Details")
        if details_box:
            paras = [_clean_text(p.get_text(" ", strip=True)) for p in details_box.find_all("p")]
            description = " ".join(p for p in paras if p)
            if description:
                result["description"] = description

        terms_box = _find_box(soup, "Terms of Sale")
        terms_p = terms_box.select_one("p.auction-terms") if terms_box else None
        if terms_p:
            terms_text = _clean_text(terms_p.get_text(" ", strip=True))
            if terms_text:
                result["extra_fields"] = f"Terms: {terms_text}"

        pdf_links = [
            clean_url(a["href"]) for a in soup.find_all("a", href=True)
            if a["href"].lower().endswith(".pdf")
        ]
        result["pdf_links"] = "; ".join(pdf_links)

        # Prefer the detail page's own primary date as authoritative, same
        # pattern as jjmanning.py -- refine, don't just trust the calendar excerpt.
        date_box = _find_box(soup, "Auction Date")
        date_strong = date_box.select_one("p strong") if date_box else None
        if date_strong:
            detail_date_text = date_strong.get_text(strip=True)
            dt = _parse_no_year_date(detail_date_text)
            if dt:
                result["date_time"] = detail_date_text
                result["auction_dt"] = dt
                result["timing"] = classify_timing(dt)

        return result