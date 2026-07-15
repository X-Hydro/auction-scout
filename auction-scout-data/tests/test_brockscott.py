"""
Tests for brockscott.py.

These test the parsing logic only (_parse_article, _parse_sale_date,
_parse_address, _find_next_url) against synthetic HTML fixtures built to
match the site's real markup pattern (.forecol label/value <p> pairs,
misleadingly-classed .pagination "nav-previous" div, etc.) -- see the
module docstring in brockscott.py for why the markup looks the way it does.

No network calls: scrape() itself (which does the live pagination loop) is
intentionally not covered here. If you add a mocked get_soup() harness for
that later, keep it in a separate slow/integration-marked test.
"""

from datetime import datetime

import pytest
from bs4 import BeautifulSoup

from brockscott import (
    BrockScottSpider,
    TARGET_STATES,
    _parse_address,
    _parse_sale_date,
)


# ---- fixtures ----------------------------------------------------------

def make_article(
        post_id="post-213",
        county="Merrimack",
        sale_date="07/17/2026 - ",
        state="NH",
        court_sp="2026-CV-00412",
        case_number="26-12789-FC01",
        address="20 High St  Concord, NH 03303",
        opening_bid="0.00",
        book_page="3802/158",
        omit_fields=(),
):
    """Build a synthetic <article> matching the real .forecol structure."""
    field_map = {
        "County": county,
        "Sale Date": sale_date,
        "State": state,
        "Court SP #": court_sp,
        "Case #": case_number,
        "Address": address,
        "Opening Bid Amount": opening_bid,
        "Book Page": book_page,
    }
    cols = ""
    for label, value in field_map.items():
        if label in omit_fields or value is None:
            continue
        cols += f'<div class="forecol"><p>{label}:</p><p>{value}</p></div>'

    html = f'<article id="{post_id}" class="foreclosure_search foreclosure_state-{state.lower()}">{cols}</article>'
    return BeautifulSoup(html, "html.parser").select_one("article")


@pytest.fixture
def spider():
    return BrockScottSpider()


# ---- county / description regression (the bug we just fixed) ----------

class TestCountyField:
    def test_county_is_clean_not_merged_with_case_info(self, spider):
        """Regression test: county must NOT contain case #/bid/book page.

        This is the exact bug seen in production: county came back as
        'Merrimack | Case #: 26-12789-FC01 | Opening Bid: 0.00 | Book Page: 3802/158'
        instead of just 'Merrimack'.
        """
        article = make_article(county="Merrimack", case_number="26-12789-FC01")
        row = spider._parse_article(article, "https://example.com/listing")

        assert row["county"] == "Merrimack"
        assert "Case #" not in row["county"]
        assert "Opening Bid" not in row["county"]
        assert "Book Page" not in row["county"]

    def test_extra_fields_contains_case_and_bid_details(self, spider):
        article = make_article(
            county="Merrimack",
            case_number="26-12789-FC01",
            opening_bid="0.00",
            book_page="3802/158",
        )
        row = spider._parse_article(article, "https://example.com/listing")

        assert "26-12789-FC01" in row["extra_fields"]
        assert "0.00" in row["extra_fields"]
        assert "3802/158" in row["extra_fields"]

    def test_missing_county_is_empty_string_not_none_or_crash(self, spider):
        article = make_article(omit_fields=("County",))
        row = spider._parse_article(article, "https://example.com/listing")
        assert row["county"] == ""


# ---- structurally unavailable fields -----------------------------------

class TestUnavailableFields:
    def test_property_detail_fields_are_none(self, spider):
        """This source has no type/beds/sqft/year-built anywhere -- confirm
        we return explicit None rather than silently dropping the keys or
        (worse) inventing a value."""
        article = make_article()
        row = spider._parse_article(article, "https://example.com/listing")

        for key in ("property_type", "bedrooms", "bathrooms", "sqft", "lot_size", "year_built"):
            assert key in row
            assert row[key] is None


# ---- state filtering -----------------------------------------------------

class TestStateFiltering:
    def test_target_state_is_kept(self, spider):
        article = make_article(state="NH")
        row = spider._parse_article(article, "https://example.com/listing")
        assert row is not None

    @pytest.mark.parametrize("state", ["NY", "PA", "FL", "AL"])
    def test_non_target_state_is_filtered_out(self, spider, state):
        article = make_article(state=state)
        row = spider._parse_article(article, "https://example.com/listing")
        assert row is None

    def test_all_target_states_accepted(self, spider):
        for state in TARGET_STATES:
            article = make_article(state=state, post_id=f"post-{state}")
            row = spider._parse_article(article, "https://example.com/listing")
            assert row is not None, f"{state} should be accepted"


# ---- id handling -----------------------------------------------------

class TestIdHandling:
    def test_missing_post_id_returns_none(self, spider):
        article = make_article(post_id="")
        # simulate an article with no id attribute at all
        del article["id"]
        row = spider._parse_article(article, "https://example.com/listing")
        assert row is None

    def test_post_id_prefix_is_stripped(self, spider):
        article = make_article(post_id="post-213")
        row = spider._parse_article(article, "https://example.com/listing")
        assert row["id"] == "213"


# ---- detail URL fallback -----------------------------------------------

class TestDetailUrl:
    def test_case_number_present_builds_deep_link(self, spider):
        article = make_article(case_number="26-12789-FC01")
        row = spider._parse_article(article, "https://example.com/listing")
        assert "_sfm_casenumber=26-12789-FC01" in row["url"]

    def test_case_number_missing_falls_back_to_listing_url(self, spider):
        article = make_article(omit_fields=("Case #",))
        row = spider._parse_article(article, "https://example.com/listing-page")
        assert row["url"] == "https://example.com/listing-page"

    def test_case_number_with_special_chars_is_url_quoted(self, spider):
        # quote()'s default safe='/' leaves slashes intact but still encodes
        # spaces -- assert on the space encoding, which is the part that
        # actually matters (an un-encoded space would break the query string).
        article = make_article(case_number="26/12-FC 01")
        row = spider._parse_article(article, "https://example.com/listing")
        assert "26/12-FC%2001" in row["url"]


# ---- address parsing -----------------------------------------------------

class TestAddressParsing:
    def test_standard_address_splits_street_and_city_state_zip(self):
        street, city_state = _parse_address("20 High St  Concord, NH 03303", "NH")
        assert street == "20 High St"
        assert city_state == "Concord, NH 03303"

    def test_address_missing_zip_degrades_gracefully(self):
        street, city_state = _parse_address("5 Main St  Burlington, VT", "VT")
        assert street == "5 Main St"
        assert "Burlington" in city_state

    def test_malformed_address_falls_back_to_raw(self):
        street, city_state = _parse_address("garbled no comma no double space", "MA")
        assert street == "garbled no comma no double space"
        assert city_state == "MA"

    def test_empty_address_does_not_crash(self):
        street, city_state = _parse_address("", "RI")
        assert street == ""
        assert city_state == "RI"


# ---- sale date parsing -----------------------------------------------------

class TestSaleDateParsing:
    def test_standard_date_with_trailing_dash_placeholder(self):
        # dateutil fills any field absent from the input (here: time-of-day)
        # from `default=datetime.now()`, so only the date portion is
        # deterministic -- asserting full equality against midnight would be
        # flaky depending on what time the test runs.
        dt, timing = _parse_sale_date("07/17/2026 - ")
        assert dt.date() == datetime(2026, 7, 17).date()
        assert timing != "Unknown"

    def test_date_without_trailing_dash(self):
        dt, timing = _parse_sale_date("07/17/2026")
        assert dt.date() == datetime(2026, 7, 17).date()

    def test_empty_date_returns_unknown(self):
        dt, timing = _parse_sale_date("")
        assert dt is None
        assert timing == "Unknown"

    def test_garbage_date_returns_unknown_not_crash(self):
        dt, timing = _parse_sale_date("not a real date!!")
        assert dt is None
        assert timing == "Unknown"


# ---- pagination -----------------------------------------------------

class TestPaginationNextLink:
    def test_finds_next_link_by_text_ignoring_misleading_class(self, spider):
        html = '''
        <div class="pagination">
            <div class="nav-previous"><a href="/foreclosure-sales/page/2/">Next &gt;</a></div>
        </div>
        '''
        soup = BeautifulSoup(html, "html.parser")
        assert spider._find_next_url(soup) == "/foreclosure-sales/page/2/"

    def test_no_next_link_on_last_page(self, spider):
        html = '<div class="pagination"><a href="/foreclosure-sales/page/1/">&lt; Prev</a></div>'
        soup = BeautifulSoup(html, "html.parser")
        assert spider._find_next_url(soup) is None

    def test_no_pagination_at_all(self, spider):
        soup = BeautifulSoup("<div>no pagination here</div>", "html.parser")
        assert spider._find_next_url(soup) is None


# ---- full listing parse (multiple articles) -----------------------------

class TestParseListing:
    def test_mixed_states_only_target_states_returned(self, spider):
        html = (
                str(make_article(post_id="post-1", state="NH"))
                + str(make_article(post_id="post-2", state="NY"))
                + str(make_article(post_id="post-3", state="MA"))
        )
        soup = BeautifulSoup(f"<div>{html}</div>", "html.parser")
        rows = spider.parse_listing(soup, "https://example.com/listing")
        assert len(rows) == 2
        assert {r["id"] for r in rows} == {"1", "3"}