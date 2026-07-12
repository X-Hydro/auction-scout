"""
AI-enhanced Sullivan spider.

Subclasses the real spiders.sullivan.SullivanSpider and overrides ONLY
parse_detail() -- listing_urls() and parse_listing() are inherited
unchanged. That means the discovery logic (which pages to hit, how to
find id/url/date_time/status/street/city_state) lives in exactly one
place: spiders/sullivan.py. Fix a bug there and both run-scout.py and
run-scout-ai.py pick it up automatically -- nothing to keep in sync here.
"""
from spiders.sullivan import SullivanSpider
from base import clean_url
from ai_property_extractor import extract_property_specs, PropertySpecs


class SullivanAISpider(SullivanSpider):
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

        # County is a clean, unambiguous single field -- pull it out
        # deterministically, no need to spend a model call resolving it.
        county = fields.pop("County", "")

        fields_text = "; ".join(f"{k}: {v}" for k, v in fields.items())
        try:
            specs = extract_property_specs(
                fields_text,
                source_site=self.name,
                cache_key=f"{self.name}:{row.get('id', row['url'])}",
            )
        except Exception as e:
            print(f"[{self.name}] AI property extraction failed on {row['url']}: {e}")
            specs = PropertySpecs()

        return {
            "pdf_links": "; ".join(pdf_links),
            "county": county,
            "property_type": specs.property_type,
            "bedrooms": specs.bedrooms,
            "bathrooms": specs.bathrooms,
            "sqft": specs.sqft,
            "lot_size": specs.lot_size,
            "year_built": specs.year_built,
            "extra_fields": specs.extra_fields,
        }