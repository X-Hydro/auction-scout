"""
AI-enhanced JJManning spider.

Subclasses the real spiders.jjmanning.JJManningSpider. Reuses its
_acf_fields()/_parse_address()/_description_text() helpers and
listing_urls()/parse_listing() unchanged -- only parse_detail() differs,
adding property-spec extraction from the description prose that the
non-AI version leaves unparsed.
"""
from spiders.jjmanning import JJManningSpider, _acf_fields, _parse_address, _description_text
from base import clean_url
from ai_property_extractor import extract_property_specs, PropertySpecs


class JJManningAISpider(JJManningSpider):
    def parse_detail(self, soup, row):
        fields = _acf_fields(soup)

        street, city_state = _parse_address(fields.get("property_address", ""))
        description = _description_text(soup) or row.get("description", "")

        pdf_links = [
            clean_url(a["href"]) for a in soup.find_all("a", href=True)
            if a["href"].lower().endswith(".pdf")
        ]

        try:
            specs = extract_property_specs(
                description,
                source_site=self.name,
                cache_key=f"{self.name}:{row['id']}",
            )
        except Exception as e:
            print(f"[{self.name}] AI property extraction failed on {row['url']}: {e}")
            specs = PropertySpecs()

        extra_parts = [
            f"Ref #: {fields['auction_reference_number']}" if fields.get("auction_reference_number") else "",
            f"Viewing: {fields['viewing_date']}" if fields.get("viewing_date") else "",
            f"Terms: {fields['property_terms']}" if fields.get("property_terms") else "",
            specs.extra_fields or "",
            ]

        result = {
            "street": street,
            "city_state": city_state,
            "description": description,
            "extra_fields": " | ".join(p for p in extra_parts if p),
            "pdf_links": "; ".join(pdf_links),
            "property_type": specs.property_type,
            "bedrooms": specs.bedrooms,
            "bathrooms": specs.bathrooms,
            "sqft": specs.sqft,
            "lot_size": specs.lot_size,
            "year_built": specs.year_built,
        }
        if fields.get("auction_date"):
            result["date_time"] = fields["auction_date"]
        return result