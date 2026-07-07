"""Sullivan & Sullivan Auctioneers (sullivan-auctioneers.com) -- MA/NH/RI."""

import re
from urllib.parse import urljoin

from base import AuctionSpider


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
            a["href"] for a in soup.find_all("a", href=True)
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

        return {
            "pdf_links": "; ".join(pdf_links),
            "extra_fields": "; ".join(f"{k}: {v}" for k, v in fields.items()),
        }