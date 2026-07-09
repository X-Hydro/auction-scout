"""
Tests for harmon.py -- _extract_labeled_date(), _parse_registry(), the
date/status branching inside parse_listing(), the small parsing helpers,
and _discover_upper_bound()'s probe logic.

HarmonSpider()'s __init__ makes a live robots.txt request (inherited from
AuctionSpider). As in test_sullivan.py, we bypass it with __new__ for the
pure-parsing tests. _discover_upper_bound() also needs self.request_delay,
which __init__ would normally set -- tests that call it set that attribute
by hand and mock get_soup instead of hitting the network.
"""

from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup

from harmon import (
    HarmonSpider,
    HARMON_ID_END,
    PROBE_MAX_EXTRA,
    PROBE_MISS_THRESHOLD,
    _extract_labeled_date,
    _parse_registry,
)


@pytest.fixture
def spider():
    return HarmonSpider.__new__(HarmonSpider)


def _soup(html):
    return BeautifulSoup(html, "html.parser")


# ---- _extract_labeled_date -------------------------------------------

class TestExtractLabeledDate:
    def test_finds_begins_date(self):
        text = "Begins: 07/15/2026 at 10:00 AM"
        assert _extract_labeled_date(text, "Begins") == "07/15/2026 at 10:00 AM"

    def test_finds_postponed_to_date_distinct_from_original_date(self):
        text = (
            "Original Date: 06/29/2026 at 12:00 AM "
            "Postponed To: 08/31/2026 at 10:00 AM"
        )
        assert _extract_labeled_date(text, "Postponed To") == "08/31/2026 at 10:00 AM"
        assert _extract_labeled_date(text, "Original Date") == "06/29/2026 at 12:00 AM"

    def test_label_not_present_returns_none(self):
        text = "Begins: 07/15/2026 at 10:00 AM"
        assert _extract_labeled_date(text, "Postponed To") is None

    def test_label_present_without_matching_numeric_date_returns_none(self):
        # e.g. "Original Date" mentioned but text-month format, or garbled
        text = "Original Date: TBD"
        assert _extract_labeled_date(text, "Original Date") is None

    def test_case_insensitive_label_match(self):
        text = "BEGINS: 01/02/2027 at 9:00 AM"
        assert _extract_labeled_date(text, "Begins") == "01/02/2027 at 9:00 AM"

    def test_single_digit_month_day_hour(self):
        text = "Begins: 1/2/2027 at 9:00 AM"
        assert _extract_labeled_date(text, "Begins") == "1/2/2027 at 9:00 AM"

    def test_empty_text_returns_none(self):
        assert _extract_labeled_date("", "Begins") is None


# ---- _parse_registry ---------------------------------------------------

class TestParseRegistry:
    def test_full_format(self):
        text = "Registry: Worcester Book / Page: 57166 / 202"
        county, book_page = _parse_registry(text)
        assert county == "Worcester"
        assert book_page == "57166 / 202"

    def test_missing_registry_prefix_falls_back_to_county(self):
        text = "Worcester Book / Page: 57166 / 202"
        county, book_page = _parse_registry(text)
        assert county == "Worcester"
        assert book_page == "57166 / 202"

    def test_missing_book_page_keeps_whole_remainder_as_county(self):
        text = "Registry: Worcester"
        county, book_page = _parse_registry(text)
        assert county == "Worcester"
        assert book_page == ""

    def test_empty_string_returns_blank_tuple(self):
        assert _parse_registry("") == ("", "")

    def test_none_input_returns_blank_tuple_not_crash(self):
        assert _parse_registry(None) == ("", "")


# ---- parse_listing: date/status branching (end-to-end) ----------------

def _block_html(inner):
    return f"""
    <div class="blockAuction">
      <h1>Auction Details</h1>
      <h2>Foreclosure Sale</h2>
      <div class="location">Location: 128 North Street <span>East Brookfield, MA</span></div>
      <div class="registry">Registry: Worcester Book / Page: 57166 / 202</div>
      {inner}
    </div>
    """


class TestParseListingDateStatus:
    def test_active_auction_uses_begins_date(self, spider):
        html = _block_html(
            '<div class="timedescription">Begins: 07/15/2026 at 10:00 AM</div>'
            '<input name="AuctionID" value="1950">'
        )
        rows = spider.parse_listing(_soup(html), "https://www.harmonlawoffices.com/auction/1950")
        assert len(rows) == 1
        assert rows[0]["date_time"] == "07/15/2026 at 10:00 AM"
        assert rows[0]["status"] == "active"

    def test_postponed_auction_prefers_postponed_to_over_original_date(self, spider):
        html = _block_html(
            '<div class="timedescription">'
            "Original Date: 06/29/2026 at 12:00 AM "
            "Postponed To: 08/31/2026 at 10:00 AM"
            "</div>"
            '<input name="AuctionID" value="1951">'
        )
        rows = spider.parse_listing(_soup(html), "https://www.harmonlawoffices.com/auction/1951")
        assert rows[0]["date_time"] == "08/31/2026 at 10:00 AM"
        assert rows[0]["status"] == "postponed"
        # original date preserved for the record, not lost
        assert "Original Date: 06/29/2026 at 12:00 AM" in rows[0]["extra_fields"]

    def test_postponed_with_no_new_date_yet(self, spider):
        html = _block_html(
            '<div class="timedescription">Original Date: 06/29/2026 at 12:00 AM</div>'
            '<input name="AuctionID" value="1952">'
        )
        rows = spider.parse_listing(_soup(html), "https://www.harmonlawoffices.com/auction/1952")
        assert rows[0]["date_time"] == ""
        assert rows[0]["status"] == "postponed"

    def test_completed_auction_has_no_date_and_uses_status_phrase(self, spider):
        html = _block_html(
            '<div class="timedescription">sold back to mortgagee</div>'
            '<input name="AuctionID" value="1953">'
        )
        rows = spider.parse_listing(_soup(html), "https://www.harmonlawoffices.com/auction/1953")
        assert rows[0]["date_time"] == ""
        assert rows[0]["status"] == "sold back to mortgagee"

    def test_completed_status_is_lowercased(self, spider):
        html = _block_html(
            '<div class="timedescription">3RD PARTY PURCHASE</div>'
            '<input name="AuctionID" value="1954">'
        )
        rows = spider.parse_listing(_soup(html), "https://www.harmonlawoffices.com/auction/1954")
        assert rows[0]["status"] == "3rd party purchase"

    def test_no_date_and_blank_timedescription_falls_back_to_unknown(self, spider):
        html = _block_html('<input name="AuctionID" value="1955">')
        rows = spider.parse_listing(_soup(html), "https://www.harmonlawoffices.com/auction/1955")
        assert rows[0]["status"] == "unknown"

    def test_missing_blockauction_h1_returns_empty_list(self, spider):
        html = '<div class="blockAuction"><p>no h1 here</p></div>'
        rows = spider.parse_listing(_soup(html), "https://www.harmonlawoffices.com/auction/9999")
        assert rows == []

    def test_missing_blockauction_entirely_returns_empty_list(self, spider):
        rows = spider.parse_listing(_soup("<div>nothing</div>"), "https://www.harmonlawoffices.com/auction/9999")
        assert rows == []

    def test_deposit_and_book_page_land_in_extra_fields(self, spider):
        html = _block_html(
            '<div class="timedescription">Begins: 07/15/2026 at 10:00 AM</div>'
            "<strong>Deposit: $5,000</strong>"
            '<input name="AuctionID" value="1956">'
        )
        rows = spider.parse_listing(_soup(html), "https://www.harmonlawoffices.com/auction/1956")
        assert "Deposit: $5,000" in rows[0]["extra_fields"]
        assert "Book/Page: 57166 / 202" in rows[0]["extra_fields"]


# ---- id / location / deposit helpers -----------------------------------

class TestExtractId:
    def test_prefers_hidden_input_value(self, spider):
        soup = _soup('<input name="AuctionID" value="1950">')
        assert spider._extract_id(soup, "https://www.harmonlawoffices.com/auction/9999") == "1950"

    def test_falls_back_to_url_when_no_hidden_input(self, spider):
        soup = _soup("<div>no input here</div>")
        assert spider._extract_id(soup, "https://www.harmonlawoffices.com/auction/1950") == "1950"

    def test_returns_none_when_neither_source_available(self, spider):
        soup = _soup("<div>no input here</div>")
        assert spider._extract_id(soup, "https://www.harmonlawoffices.com/not-an-auction-url") is None


class TestParseLocation:
    def test_splits_street_and_city_state(self, spider):
        block = _soup(
            '<div class="location">Location: 128 North Street <span>East Brookfield, MA</span></div>'
        )
        street, city_state = spider._parse_location(block)
        assert street == "128 North Street"
        assert city_state == "East Brookfield, MA"

    def test_missing_location_element_returns_blank_tuple(self, spider):
        block = _soup("<div>nothing</div>")
        assert spider._parse_location(block) == ("", "")


class TestParseDeposit:
    def test_finds_deposit_in_strong_tag(self, spider):
        block = _soup("<div><strong>Deposit: $5,000</strong></div>")
        assert spider._parse_deposit(block) == "$5,000"

    def test_ignores_unrelated_strong_tags(self, spider):
        block = _soup("<div><strong>Important Notice</strong></div>")
        assert spider._parse_deposit(block) == ""

    def test_no_strong_tags_returns_blank(self, spider):
        block = _soup("<div>nothing bold here</div>")
        assert spider._parse_deposit(block) == ""


# ---- _discover_upper_bound: probe logic --------------------------------

def _hit_soup():
    return _soup('<div class="blockAuction"><h1>Auction</h1></div>')


def _miss_soup():
    return _soup("<div>nothing</div>")


class TestDiscoverUpperBound:
    def test_stops_at_threshold_when_nothing_new(self, spider):
        spider.request_delay = 0
        with patch.object(spider, "get_soup", return_value=_miss_soup()) as mock_get:
            end = spider._discover_upper_bound()
        assert end == HARMON_ID_END
        assert mock_get.call_count == PROBE_MISS_THRESHOLD

    def test_extends_range_when_new_auctions_exist(self, spider):
        spider.request_delay = 0
        # ids END+1 and END+2 are hits, then PROBE_MISS_THRESHOLD misses in a row
        responses = [_hit_soup(), _hit_soup()] + [_miss_soup()] * PROBE_MISS_THRESHOLD
        with patch.object(spider, "get_soup", side_effect=responses):
            end = spider._discover_upper_bound()
        assert end == HARMON_ID_END + 2

    def test_nonconsecutive_miss_does_not_end_probe_early(self, spider):
        spider.request_delay = 0
        # miss, hit, then PROBE_MISS_THRESHOLD misses -- the single miss
        # before the hit must NOT count toward ending the probe.
        responses = [_miss_soup(), _hit_soup()] + [_miss_soup()] * PROBE_MISS_THRESHOLD
        with patch.object(spider, "get_soup", side_effect=responses):
            end = spider._discover_upper_bound()
        assert end == HARMON_ID_END + 2  # the id at index 1 (second probe) was the hit

    def test_probe_max_extra_caps_requests_when_never_hitting_threshold(self, spider):
        spider.request_delay = 0
        # A hit every 3rd probe means misses_in_a_row never reaches
        # PROBE_MISS_THRESHOLD, so the loop should run until PROBE_MAX_EXTRA.
        def cycling_response(*args, **kwargs):
            cycling_response.n += 1
            return _hit_soup() if cycling_response.n % 3 == 0 else _miss_soup()
        cycling_response.n = 0

        with patch.object(spider, "get_soup", side_effect=cycling_response) as mock_get:
            spider._discover_upper_bound()
        assert mock_get.call_count == PROBE_MAX_EXTRA

    def test_exception_from_get_soup_is_treated_as_a_miss(self, spider):
        spider.request_delay = 0
        with patch.object(spider, "get_soup", side_effect=RuntimeError("boom")) as mock_get:
            end = spider._discover_upper_bound()
        assert end == HARMON_ID_END
        assert mock_get.call_count == PROBE_MISS_THRESHOLD