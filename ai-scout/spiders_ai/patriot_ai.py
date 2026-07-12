"""
AI-enhanced Patriot spider.

Subclasses the real spiders.patriot.PatriotSpider. Reuses its
_find_box()/_clean_text()/_parse_no_year_date() helpers and
listing_urls()/parse_listing() unchanged -- only parse_detail() differs,
adding property-spec extraction from the "Property Details" box prose
that the non-AI version leaves unparsed.
"""
from spiders.patriot import PatriotSpider, _find_box, _clean_text, _parse_no_year_date
from base import classify_timing, clean_url
from ai_property_extractor import extract_property_specs, PropertySpecs


class PatriotAISpider(PatriotSpider):
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
        description = ""
        if details_box:
            paras = [_clean_text(p.get_text(" ", strip=True)) for p in details_box.find_all("p")]
            description = " ".join(p for p in paras if p)
            if description:
                result["description"] = description

        if description:
            try:
                specs = extract_property_specs(
                    description,
                    source_site=self.name,
                    cache_key=f"{self.name}:{row['id']}",
                )
            except Exception as e:
                print(f"[{self.name}] AI property extraction failed on {row['url']}: {e}")
                specs = PropertySpecs()
            result["property_type"] = specs.property_type
            result["bedrooms"] = specs.bedrooms
            result["bathrooms"] = specs.bathrooms
            result["sqft"] = specs.sqft
            result["lot_size"] = specs.lot_size
            result["year_built"] = specs.year_built
            ai_extra = specs.extra_fields
        else:
            ai_extra = None

        terms_box = _find_box(soup, "Terms of Sale")
        terms_p = terms_box.select_one("p.auction-terms") if terms_box else None
        terms_text = _clean_text(terms_p.get_text(" ", strip=True)) if terms_p else ""

        extra_parts = [f"Terms: {terms_text}" if terms_text else "", ai_extra or ""]
        combined_extra = " | ".join(p for p in extra_parts if p)
        if combined_extra:
            result["extra_fields"] = combined_extra

        pdf_links = [
            clean_url(a["href"]) for a in soup.find_all("a", href=True)
            if a["href"].lower().endswith(".pdf")
        ]
        result["pdf_links"] = "; ".join(pdf_links)

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