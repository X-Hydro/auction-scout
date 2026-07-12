"""Sullivan & Sullivan Auctioneers (sullivan-auctioneers.com) -- MA/NH/RI."""

import re
from urllib.parse import urljoin

from base import AuctionSpider, clean_url

# Matches a run of digits (with optional thousands commas) anywhere in a
# value like "2,860± sf" or "864 sf" or "6" -- used to pull a plain int out
# of Square Feet / Bedrooms / Year Built values that carry units or a "±"
# precision marker alongside the number.
_DIGITS_RE = re.compile(r"[\d,]+")


def _first_int(value):
    """Pull the first integer out of value, or None if there's no usable
    digit run. NOT used for bathrooms -- see _extract_property_details."""
    if not value:
        return None
    m = _DIGITS_RE.search(value)
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _extract_property_details(fields):
    """
    Route the labeled <li> fields (Property Type, Lot Size, Square Feet,
    # Bedrooms, # Baths, Year Built, County, ...) into the structured row
    keys every spider is expected to provide, leaving anything
    unrecognized (Mortgage Ref, # Rooms, etc.) as free text in
    extra_fields -- same parse-once-at-the-source shape as
    spiders/brockscott.py, instead of shipping raw label text downstream
    for some other script to re-parse.

    `fields` keys carry their original label text verbatim, including a
    leading '#' for count-style fields ('# Bedrooms', '# Baths', '# Rooms')
    -- matched both with and without that prefix since it's not guaranteed
    consistent across every listing.

    Not every listing includes every label (County in particular is
    sometimes absent -- see spiders/sullivan.py callers/tests), so every
    lookup here has to tolerate a miss.
    """
    remaining = dict(fields)  # working copy; take() pops as it consumes

    def take(*labels):
        for label in labels:
            if label in remaining:
                return remaining.pop(label)
        return None

    county = take("County")
    property_type = take("Property Type")
    lot_size = take("Lot Size")
    sqft_raw = take("Square Feet", "Sq Ft", "Sq. Ft.")
    bedrooms_raw = take("# Bedrooms", "Bedrooms")
    # Baths intentionally kept as raw text, NOT coerced to an int --
    # listings like "3 full / 2 half" lose real information if forced into
    # a single number. Downstream consumers that want a plain count can
    # parse the leading digit themselves; we don't want to make that
    # (lossy) choice here at the source.
    bathrooms_raw = take("# Baths", "Baths")
    year_built_raw = take("Year Built")

    return {
        "county": county or "",
        "property_type": property_type,
        "lot_size": lot_size,
        "sqft": _first_int(sqft_raw),
        "bedrooms": _first_int(bedrooms_raw),
        "bathrooms": bathrooms_raw,
        "year_built": _first_int(year_built_raw),
        # Whatever's left over (Mortgage Ref, # Rooms, anything with no
        # dedicated column) -- still free text. Leading '#' is stripped
        # from the label (e.g. "# Rooms" -> "Rooms") so this matches the
        # AI-extracted version's formatting exactly -- the AI path
        # normalizes this the same way, and there's no reason for the two
        # pipelines to disagree on cosmetic label formatting when the
        # underlying fact is identical.
        "extra_fields": "; ".join(
            f"{k.lstrip('#').strip()}: {v}" for k, v in remaining.items()
        ),
    }


class SullivanSpider(AuctionSpider):
    name = "sullivan"
    base_url = "https://sullivan-auctioneers.com"

    def listing_urls(self):
        return [
            f"{self.base_url}/massachusetts/",
            f"{self.base_url}/new-hampshire/",
            f"{self.base_url}/rhode-island/",
        ]

    def parse_listing(self, soup, listing_url):
        table = soup.find("table")
        if not table:
            return []

        rows = []
        for tr in table.find_all("tr")[1:]:  # skip header row
            cells = tr.find_all("td")
            if len(cells) < 5:
                continue

            date_cell, status_cell, street_cell, city_cell, desc_cell = cells[:5]
            link = date_cell.find("a")
            if not link:
                continue

            url = urljoin(self.base_url, link["href"])
            auction_id = re.search(r"id=(\d+)", url)
            auction_id = auction_id.group(1) if auction_id else None

            rows.append({
                "id": auction_id,
                "url": url,
                "date_time": link.get_text(strip=True),
                "status": status_cell.get_text(strip=True),
                "street": street_cell.get_text(strip=True),
                "city_state": city_cell.get_text(strip=True),
                "description": desc_cell.get_text(strip=True),
            })
        return rows

    def parse_detail(self, soup, row):
        pdf_links = [
            clean_url(a["href"]) for a in soup.find_all("a", href=True)
            if a["href"].lower().endswith(".pdf")
        ]

        fields = {}
        for li in soup.find_all("li"):
            text = li.get_text(" ", strip=True)
            if ":" in text and len(text) < 120:
                label, _, value = text.partition(":")
                label = label.strip()
                if label and value.strip():
                    fields[label] = value.strip()

        details = _extract_property_details(fields)

        return {
            "pdf_links": "; ".join(pdf_links),
            **details,
        }