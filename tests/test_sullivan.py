"""
Tests for sullivan.py -- specifically _first_int(), _extract_property_details(),
and parse_detail()/parse_listing() end-to-end against synthetic HTML.

SullivanSpider()'s __init__ makes a live robots.txt request (inherited from
AuctionSpider), which we don't want in a unit test. None of the methods
tested here (parse_listing, parse_detail, the module-level helpers) touch
anything __init__ sets up, so we bypass it with __new__ rather than mocking
the network call -- simpler and makes the tests' intent clearer (these are
pure parsing tests, not integration tests of the robots.txt handshake).
"""

import pytest
from bs4 import BeautifulSoup

from sullivan import SullivanSpider, _extract_property_details, _first_int


@pytest.fixture
def spider():
    return SullivanSpider.__new__(SullivanSpider)


# ---- _first_int ----------------------------------------------------

class TestFirstInt:
    @pytest.mark.parametrize("value,expected", [
        ("864 sf", 864),
        ("2,860± sf", 2860),
        ("22,778± sf", 22778),
        ("4", 4),
        ("1953", 1953),
    ])
    def test_extracts_leading_number_ignoring_units_and_markers(self, value, expected):
        assert _first_int(value) == expected

    def test_none_input_returns_none(self):
        assert _first_int(None) is None

    def test_empty_string_returns_none(self):
        assert _first_int("") is None

    def test_no_digits_returns_none(self):
        assert _first_int("unknown") is None

    def test_fractional_baths_style_value_still_extracts_first_number(self):
        # Not how bathrooms are actually parsed (they intentionally bypass
        # _first_int -- see TestExtractPropertyDetails.test_fractional_baths_kept_as_raw_text),
        # but _first_int itself should still behave sanely if ever reused elsewhere.
        assert _first_int("3 full / 2 half") == 3


# ---- _extract_property_details --------------------------------------

class TestExtractPropertyDetails:
    def test_full_field_set_routes_correctly(self):
        fields = {
            "Property Type": "Residential",
            "Mortgage Ref": "Worcester Cty. (Worcester Dist.) in Bk 57166, Pg 202",
            "Lot Size": "1.01 Acre",
            "Square Feet": "864 sf",
            "# Bedrooms": "2",
            "# Baths": "1",
            "Year Built": "1953",
            "County": "Worcester",
        }
        result = _extract_property_details(fields)

        assert result["county"] == "Worcester"
        assert result["property_type"] == "Residential"
        assert result["lot_size"] == "1.01 Acre"
        assert result["sqft"] == 864
        assert result["bedrooms"] == 2
        assert result["bathrooms"] == "1"
        assert result["year_built"] == 1953
        # Mortgage Ref has no dedicated column -- must survive in extra_fields
        assert "Mortgage Ref" in result["extra_fields"]
        assert "Bk 57166, Pg 202" in result["extra_fields"]

    def test_missing_county_is_empty_string_not_crash(self):
        # Regression case: sullivan:20439 in production had no County label at all.
        fields = {
            "Property Type": "Residential",
            "Mortgage Ref": "Norfolk Cty. DLC on Doc #1553796 on COT #196406",
            "Lot Size": "22,778± sf",
            "Square Feet": "2,860± sf",
            "# Bedrooms": "6",
            "# Baths": "3 full / 2 half",
            "Year Built": "1915",
        }
        result = _extract_property_details(fields)
        assert result["county"] == ""
        assert result["sqft"] == 2860  # ± marker still strips cleanly

    def test_fractional_baths_kept_as_raw_text(self):
        # Regression case: sullivan:20439's "3 full / 2 half" must not be
        # silently truncated to "3" -- that's real information loss.
        fields = {"# Baths": "3 full / 2 half"}
        result = _extract_property_details(fields)
        assert result["bathrooms"] == "3 full / 2 half"

    def test_extra_rooms_field_falls_through_to_extra_fields(self):
        # Regression case: sullivan:20407/21074 both had a "# Rooms" label
        # with no dedicated column.
        fields = {
            "Mortgage Ref": "Barnstable Cty. in Bk 37020, Pg 314",
            "# Rooms": "14",
            "# Bedrooms": "6",
        }
        result = _extract_property_details(fields)
        assert result["bedrooms"] == 6
        assert "Rooms: 14" in result["extra_fields"]
        assert "Mortgage Ref" in result["extra_fields"]

    def test_label_without_hash_prefix_still_matches(self):
        # "# Bedrooms" is the common form, but take() also accepts "Bedrooms"
        # bare, in case a listing formats it differently.
        fields = {"Bedrooms": "5", "Baths": "2"}
        result = _extract_property_details(fields)
        assert result["bedrooms"] == 5
        assert result["bathrooms"] == "2"

    def test_empty_fields_dict_returns_all_blank_no_crash(self):
        result = _extract_property_details({})
        assert result["county"] == ""
        assert result["property_type"] is None
        assert result["sqft"] is None
        assert result["bedrooms"] is None
        assert result["bathrooms"] is None
        assert result["year_built"] is None
        assert result["extra_fields"] == ""

    def test_unrecognized_labels_all_land_in_extra_fields(self):
        fields = {"Some Unexpected Label": "value", "Another One": "value2"}
        result = _extract_property_details(fields)
        assert "Some Unexpected Label: value" in result["extra_fields"]
        assert "Another One: value2" in result["extra_fields"]

    def test_original_fields_dict_is_not_mutated(self):
        # take() works on a copy -- callers shouldn't see their input dict
        # drained out from under them.
        fields = {"County": "Worcester", "Mortgage Ref": "Bk 1, Pg 2"}
        _extract_property_details(fields)
        assert fields == {"County": "Worcester", "Mortgage Ref": "Bk 1, Pg 2"}


# ---- parse_detail (end-to-end through real HTML) ------------------------

class TestParseDetail:
    def test_full_detail_page(self, spider):
        html = """
        <div>
        <ul>
          <li>Property Type: Residential</li>
          <li>Mortgage Ref: Worcester Cty. (Worcester Dist.) in Bk 57166, Pg 202</li>
          <li>Lot Size: 1.01 Acre</li>
          <li>Square Feet: 864 sf</li>
          <li># Bedrooms: 2</li>
          <li># Baths: 1</li>
          <li>Year Built: 1953</li>
          <li>County: Worcester</li>
        </ul>
        <a href="/public_docs/2026/01/Foreclosure Notice.pdf">PDF</a>
        <a href="/some/page">Not a PDF</a>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        result = spider.parse_detail(soup, {"id": "21164"})

        assert result["county"] == "Worcester"
        assert result["sqft"] == 864
        assert result["bedrooms"] == 2
        assert result["bathrooms"] == "1"
        assert result["year_built"] == 1953
        # Space in the PDF filename must be percent-encoded, not dropped
        assert "Foreclosure%20Notice.pdf" in result["pdf_links"]
        # The non-PDF link must not be included
        assert "some/page" not in result["pdf_links"]

    def test_li_over_120_chars_is_ignored(self, spider):
        long_text = "Note: " + ("x" * 150)
        html = f"<ul><li>{long_text}</li><li>County: Worcester</li></ul>"
        soup = BeautifulSoup(html, "html.parser")
        result = spider.parse_detail(soup, {"id": "1"})
        assert result["county"] == "Worcester"
        assert "Note" not in result["extra_fields"]

    def test_li_without_colon_is_ignored(self, spider):
        html = "<ul><li>Just some text with no label</li><li>County: Worcester</li></ul>"
        soup = BeautifulSoup(html, "html.parser")
        result = spider.parse_detail(soup, {"id": "1"})
        assert result["county"] == "Worcester"

    def test_no_lis_at_all_returns_blank_details_not_crash(self, spider):
        soup = BeautifulSoup("<div>no list here</div>", "html.parser")
        result = spider.parse_detail(soup, {"id": "1"})
        assert result["county"] == ""
        assert result["pdf_links"] == ""


# ---- parse_listing (table row extraction) --------------------------

class TestParseListing:
    def test_extracts_rows_from_table(self, spider):
        html = """
        <table>
          <tr><th>Date</th><th>Status</th><th>Street</th><th>City</th><th>Desc</th></tr>
          <tr>
            <td><a href="/auction/?id=21164">Wed. Jul. 8, 2026 at 12 pm</a></td>
            <td>bank buy back</td>
            <td>128 North Street</td>
            <td>East Brookfield, MA</td>
            <td>Some description text</td>
          </tr>
        </table>
        """
        soup = BeautifulSoup(html, "html.parser")
        rows = spider.parse_listing(soup, "https://sullivan-auctioneers.com/massachusetts/")

        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == "21164"
        assert row["status"] == "bank buy back"
        assert row["street"] == "128 North Street"
        assert row["city_state"] == "East Brookfield, MA"
        assert "sullivan-auctioneers.com/auction/?id=21164" in row["url"]

    def test_no_table_returns_empty_list(self, spider):
        soup = BeautifulSoup("<div>no table</div>", "html.parser")
        assert spider.parse_listing(soup, "https://example.com") == []

    def test_row_with_too_few_cells_is_skipped(self, spider):
        html = """
        <table>
          <tr><th>H</th></tr>
          <tr><td>only</td><td>two cells</td></tr>
        </table>
        """
        soup = BeautifulSoup(html, "html.parser")
        assert spider.parse_listing(soup, "https://example.com") == []

    def test_row_without_link_in_date_cell_is_skipped(self, spider):
        html = """
        <table>
          <tr><th>H</th></tr>
          <tr><td>no link here</td><td>status</td><td>street</td><td>city</td><td>desc</td></tr>
        </table>
        """
        soup = BeautifulSoup(html, "html.parser")
        assert spider.parse_listing(soup, "https://example.com") == []