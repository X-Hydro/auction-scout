"""
Towne Auction (currentauctions.towneauction.com) -- MA/NH/RI foreclosure auctions.

Single ASP.NET WebForms page (GridView control, id="GridView1"), one static
table with ALL listings and every field needed -- no separate detail page,
no pagination ("Page: 1 of 1" for the full set).

Columns (confirmed from real HTML, in order): Auction Date, Time, Status,
Address (with a nested <a class="gwlink"> Google Maps link -- the link text
itself is the clean street address), City, State, Zip Code, County,
Page/Liber, Deposit, Important Information.

STATUS HANDLING:
- "Canceled" -> kept (falls into the generic "unrecognized status" branch:
  no date guessed, status passed through as-is). Previously skipped here
  entirely, which meant a cancelled listing never reached the database and
  only showed up downstream as an ambiguous "disappeared" event instead of
  a real status_change to cancelled. export_json.py's EXCLUDED_STATUSES
  (which already covers both the one-L and two-L spellings) is what keeps
  it off the live map now -- this file's only job is accurate parsing.
- "On_Time" -> kept, normalized to status "active" (matching every other
  source), date/time built from the Auction Date + Time columns
- "Postponed to M/D/YYYY" -> kept, but the EFFECTIVE date used is the one
  embedded in the status text (the new date), not the row's own Auction
  Date column (which is the stale original date)

DEDUP: postponed listings that have already been re-listed appear TWICE in
the table -- once as "Postponed to <date>" under the original date, once
as "On_Time" under the new date, same address both times (confirmed by
sampling: all 4 postponed listings in the initial sample had a matching
On_Time row). Per explicit decision: keep ONE row only, preferring the
active (formerly "On_Time") row over Postponed when both exist for the
same property. This is done here
via an address-based composite id + priority merge inside parse_listing()
itself (single page, no need for base.py's cross-page dedup machinery).
"""

import re
from datetime import datetime

from base import AuctionSpider, classify_timing

STATUS_PRIORITY = {"active": 2, "postponed": 1}


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _parse_on_time_date(date_str, time_str):
    """date_str like '07/07/26', time_str like '11:00 AM'."""
    try:
        dt = datetime.strptime(f"{date_str.strip()} {time_str.strip()}", "%m/%d/%y %I:%M %p")
    except ValueError:
        return None, "Unknown"
    return dt, classify_timing(dt)


def _parse_postponed_date(status_text):
    """Extract the NEW date from 'Postponed to 8/21/2026' -- that's the
    effective date, not the row's stale Auction Date column."""
    m = re.search(r"Postponed to (\d{1,2}/\d{1,2}/\d{4})", status_text)
    if not m:
        return None, "Unknown"
    try:
        dt = datetime.strptime(m.group(1), "%m/%d/%Y")
    except ValueError:
        return None, "Unknown"
    return dt, classify_timing(dt)


def _clean_city(raw_city):
    """'Barnstable (Hyannis)' -> 'Barnstable' -- parentheticals confuse geocoding."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", raw_city).strip()


class TowneAuctionSpider(AuctionSpider):
    name = "towne"
    base_url = "https://currentauctions.towneauction.com"
    scrape_details = False  # everything is on the one table page

    def listing_urls(self):
        return [self.base_url + "/"]

    def parse_listing(self, soup, listing_url):
        table = soup.find("table", id="GridView1")
        if not table:
            return []

        by_address = {}

        for tr in table.find_all("tr")[1:]:  # skip header row
            cells = tr.find_all("td")
            if len(cells) < 11:
                continue

            date_raw = cells[0].get_text(strip=True)
            time_raw = cells[1].get_text(strip=True)
            status_raw = cells[2].get_text(strip=True)
            street_link = cells[3].find("a")
            street = street_link.get_text(strip=True) if street_link else cells[3].get_text(strip=True)
            city = _clean_city(cells[4].get_text(strip=True))
            state = cells[5].get_text(strip=True)
            zip_code = cells[6].get_text(strip=True)
            county = cells[7].get_text(strip=True)
            page_liber = cells[8].get_text(strip=True)
            deposit = cells[9].get_text(strip=True)
            important_info = cells[10].get_text(strip=True)

            status_lower = status_raw.lower()

            if status_lower.startswith("postponed"):
                status_key = "postponed"
                auction_dt, timing = _parse_postponed_date(status_raw)
                date_time_display = status_raw  # e.g. "Postponed to 8/21/2026"
            elif status_lower.startswith("on_time") or status_lower.startswith("on time"):
                # Site's own spelling is "On_Time"; normalize to "active"
                # here, matching every other source, so nothing
                # downstream needs to know Towne spells this differently.
                status_key = "active"
                auction_dt, timing = _parse_on_time_date(date_raw, time_raw)
                date_time_display = f"{date_raw} {time_raw}"
            elif status_lower.startswith("cancel"):
                # Site spells this "Canceled" (one L); normalize to the
                # two-L "cancelled" spelling here, at the source, so the
                # CSV this spider produces already matches Harmon/Patriot
                # rather than relying on a downstream fixup.
                status_key = "cancelled"
                auction_dt, timing = None, "Unknown"
                date_time_display = status_raw
            else:
                # Unrecognized status -- keep it but don't guess a date.
                status_key = status_lower
                auction_dt, timing = None, "Unknown"
                date_time_display = status_raw

            addr_id = _slug(f"{street}-{city}-{state}-{zip_code}")

            print(f"towne property: {street}-{city}-{state}-{zip_code}")

            description_parts = [
                f"County: {county}" if county else "",
                f"Page/Liber: {page_liber}" if page_liber and page_liber != "\xa0" else "",
            ]
            extra_parts = [
                f"Deposit: {deposit}" if deposit and deposit != "\xa0" else "",
                f"Info: {important_info}" if important_info and important_info != "\xa0" else "",
            ]

            row = {
                "id": addr_id,
                "url": listing_url,  # no per-listing detail page on this site
                "date_time": date_time_display,
                "auction_dt": auction_dt,
                "timing": timing,
                "status": status_key,
                "street": street,
                "city_state": f"{city}, {state} {zip_code}".strip(),
                "description": " | ".join(p for p in description_parts if p),
                "extra_fields": " | ".join(p for p in extra_parts if p),
                "pdf_links": "",
            }

            existing = by_address.get(addr_id)
            if existing is None:
                by_address[addr_id] = row
            else:
                existing_priority = STATUS_PRIORITY.get(existing["status"], 0)
                new_priority = STATUS_PRIORITY.get(status_key, 0)
                if new_priority > existing_priority:
                    by_address[addr_id] = row
                # else: keep existing (higher or equal priority already there)

        return list(by_address.values())