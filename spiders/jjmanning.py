"""
J.J. Manning Auctioneers (jjmanning.com) -- MA/CT/RI real estate auctions.

APPROACH: single static calendar page for discovery + link filtering,
detail page hop for full data (address, terms, PDF links).

UPCOMING vs. DECIDED (Under Agreement / SOLD / SOLD FOR $X) filtering:
The calendar page renders each listing as an <article class="... type-auction
...">. Decided listings (Under Agreement/SOLD/etc.) structurally LACK the
date icon-box widget entirely (Elementor conditional visibility hides it --
verified in raw HTML via an "<!-- dce invisible element ... -->" comment
where it would be). Still-upcoming listings (including "Selling Absolute
Above $100K" labeled ones -- that's a sale-TYPE note, not a completion
status) always have it. So: presence of the date icon-box link is used as
the upcoming/decided signal, NOT text-matching against status strings --
more robust against wording changes ("SOLD FOR 75MM" etc.).

No pagination -- confirmed the calendar page renders everything (upcoming
AND historical back to ~2021) in one static page, all in the initial HTML
(no separate listing+detail split needed for discovery, only for full data).

Detail page fields are Elementor/ACF widgets. Each is:
  <div class="... elementor-widget-dyncontel-acf" data-settings='{"acf_field_list":"FIELD_NAME",...}'>
    <div class="elementor-widget-container">
      <div class="dynamic-content-for-elementor-acf">VALUE</div>
    </div>
  </div>
Extracted by parsing data-settings JSON directly rather than matching
visible label text (labels have inconsistent whitespace, e.g. " VIEWING
DATE"), keying off ACF field names instead: property_address,
auction_reference_number, auction_date, viewing_date, property_terms.

property_terms is the deposit/payment-terms text -- this is the field
Brock & Scott's site doesn't have at all (see spiders/brockscott.py).

Date format ("Tuesday, July 14, 2026 12:00 pm") does NOT match base.py's
DATE_CHUNK_RE (no literal "at"), but base.parse_auction_date()'s fuzzy
dateutil fallback (used when the regex finds no match) handles it correctly
anyway -- confirmed by direct test, so no site-specific date parser needed
here (unlike Brock & Scott's numeric MM/DD/YYYY format).
"""

import json
import re

from base import AuctionSpider, clean_url


def _acf_fields(soup):
    """Extract all ACF-backed fields on a detail page as {field_name: value}."""
    fields = {}
    for div in soup.select("div.elementor-widget-dyncontel-acf"):
        raw_settings = div.get("data-settings", "")
        try:
            settings = json.loads(raw_settings)
        except (ValueError, TypeError):
            continue
        field_name = settings.get("acf_field_list")
        if not field_name:
            continue
        value_div = div.select_one(".dynamic-content-for-elementor-acf")
        fields[field_name] = value_div.get_text(" ", strip=True) if value_div else ""
    return fields


def _parse_address(raw):
    """'169 Scantic Rd., Hampden, MA' -> ('169 Scantic Rd.', 'Hampden, MA')"""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) >= 3:
        street = parts[0]
        city_state = f"{parts[1]}, {parts[2]}"
    elif len(parts) == 2:
        street, city_state = parts[0], parts[1]
    else:
        street, city_state = raw.strip(), ""
    return street, city_state


def _description_text(soup):
    content = soup.select_one("div.dce-content-wrapper")
    if not content:
        return ""
    paras = [p.get_text(" ", strip=True) for p in content.find_all("p")]
    return " ".join(p for p in paras if p)


class JJManningSpider(AuctionSpider):
    name = "jjmanning"
    base_url = "https://jjmanning.com"
    scrape_details = True  # calendar page has no full address -- need the hop

    def listing_urls(self):
        return [f"{self.base_url}/auction-calendar/"]

    def parse_listing(self, soup, listing_url):
        rows = []
        for article in soup.select('article[class*="type-auction"]'):
            title_a = article.select_one("h3.elementor-heading-title a")
            if not title_a or not title_a.get("href"):
                continue

            # Structural upcoming/decided signal -- see module docstring.
            date_a = article.select_one("h4.elementor-icon-box-title a")
            if not date_a:
                continue  # Under Agreement / SOLD / decided -- skip

            detail_url = title_a["href"]
            m = re.search(r"/auction/([^/]+)/?", detail_url)
            auction_id = m.group(1) if m else None
            if not auction_id:
                continue

            status_h4 = article.select_one("h4.elementor-heading-title")
            status_text = status_h4.get_text(strip=True) if status_h4 else ""

            rows.append({
                "id": auction_id,
                "url": detail_url,
                "date_time": date_a.get_text(strip=True),  # refined in parse_detail
                "status": status_text.lower() if status_text else "active",
                "street": "",       # filled in by parse_detail
                "city_state": "",   # filled in by parse_detail
                "description": title_a.get_text(strip=True),  # placeholder
                "extra_fields": "",
                "pdf_links": "",
            })
        return rows

    def parse_detail(self, soup, row):
        fields = _acf_fields(soup)

        street, city_state = _parse_address(fields.get("property_address", ""))
        description = _description_text(soup) or row.get("description", "")

        pdf_links = [
            clean_url(a["href"]) for a in soup.find_all("a", href=True)
            if a["href"].lower().endswith(".pdf")
        ]

        extra_parts = [
            f"Ref #: {fields['auction_reference_number']}" if fields.get("auction_reference_number") else "",
            f"Viewing: {fields['viewing_date']}" if fields.get("viewing_date") else "",
            f"Terms: {fields['property_terms']}" if fields.get("property_terms") else "",
        ]

        result = {
            "street": street,
            "city_state": city_state,
            "description": description,
            "extra_fields": " | ".join(p for p in extra_parts if p),
            "pdf_links": "; ".join(pdf_links),
        }
        # Prefer the detail page's own auction_date field if present -- more
        # authoritative than the calendar page's, though they should agree.
        if fields.get("auction_date"):
            result["date_time"] = fields["auction_date"]
        return result