"""
Tests for ct_judicial.py -- parse_listing() against the real Fairfield
town page (the only real sample seen so far), plus _normalize_datetime()
and _split_address() directly.

CTJudicialSpider()'s __init__ makes a live robots.txt request (inherited
from AuctionSpider) -- as in test_sullivan.py/test_harmon.py, bypassed with
__new__ since parse_listing() doesn't touch anything __init__ sets up.

Scope, deliberately kept narrow: this spider isn't in run-scout.py's
REGISTRY yet (sso.eservices.jud.ct.gov's robots.txt disallows non-Google
bots -- see ct_judicial.py's module docstring), so there's no live-site
markup to test beyond the one real page a human has actually pulled up in
a browser and pasted in (Fairfield, 2026-08-10, 3 listings). No synthetic
edge-case fixtures (empty town, pagination, malformed address) -- add
those when real HTML surfaces a real case to test, not before.
"""

import pytest
from bs4 import BeautifulSoup

from ct_judicial import CTJudicialSpider, _normalize_datetime, _split_address


@pytest.fixture
def spider():
    return CTJudicialSpider.__new__(CTJudicialSpider)


# Trimmed to the actual GridView table -- real markup, pasted from
# sso.eservices.jud.ct.gov/foreclosures/Public/PendPostbyTownDetails.aspx?town=Fairfield
# on 2026-08-10 (page chrome/viewstate/aspnetForm boilerplate stripped,
# table contents untouched).
FAIRFIELD_HTML = """
<table id="ctl00_cphBody_GridView1">
    <tr style="color:#E7E7FF;background-color:#4A3C8C;font-weight:bold;">
        <th scope="col">#</th><th scope="col">Sale Date</th><th scope="col">Docket Number</th><th scope="col">Type of Sale & Property Address</th><th scope="col">&nbsp;</th>
    </tr><tr style="color:Black;background-color:#DEDFDE;">
        <td>1</td><td>
                   <span id="ctl00_cphBody_GridView1_ctl02_Label1">08/15/2026<br>12:00PM</span>
                </td><td><a href="PendPostbyDocketNo.aspx?DocketNo=FBTCV256151688S">FBTCV256151688S</a></td><td>
                    <span id="ctl00_cphBody_GridView1_ctl02_Label2">PUBLIC AUCTION FORECLOSURE SALE: Residential <br> ADDRESS: 736 Old Stratfield Road Fairfield CT</span>
                </td><td><a href="PendPostDetailPublic.aspx?PostingId=61252">View Full Notice</a></td>
    </tr><tr style="color:Black;background-color:#DEDFDE;">
        <td>2</td><td>
                   <span id="ctl00_cphBody_GridView1_ctl03_Label1">08/15/2026<br>12:00PM</span>
                </td><td><a href="PendPostbyDocketNo.aspx?DocketNo=FBTCV246133765S">FBTCV246133765S</a></td><td>
                    <span id="ctl00_cphBody_GridView1_ctl03_Label2">PUBLIC AUCTION FORECLOSURE SALE:Residential <br> ADDRESS:  325 Oldfield Road, Fairfield, CT 06824</span>
                </td><td><a href="PendPostDetailPublic.aspx?PostingId=61304">View Full Notice</a></td>
    </tr><tr style="color:Black;background-color:#DEDFDE;">
        <td>3</td><td>
                   <span id="ctl00_cphBody_GridView1_ctl04_Label1">08/29/2026<br>12:00PM</span>
                </td><td><a href="PendPostbyDocketNo.aspx?DocketNo=FBTCV256152305S">FBTCV256152305S</a></td><td>
                    <span id="ctl00_cphBody_GridView1_ctl04_Label2">PUBLIC AUCTION Judgment Of Partition By Sale: Residential Condominium <br> ADDRESS: 781 Fairfield Beach Road, Fairfield CT</span>
                </td><td><a href="PendPostDetailPublic.aspx?PostingId=61355">View Full Notice</a></td>
    </tr>
</table>
"""

FAIRFIELD_URL = "https://sso.eservices.jud.ct.gov/foreclosures/Public/PendPostbyTownDetails.aspx?town=Fairfield"


def _soup(html):
    return BeautifulSoup(html, "html.parser")


# ---- parse_listing (end-to-end through real HTML) ----------------------

class TestParseListing:
    def test_extracts_all_three_rows(self, spider):
        rows = spider.parse_listing(_soup(FAIRFIELD_HTML), FAIRFIELD_URL)
        assert len(rows) == 3
        assert {r["id"] for r in rows} == {
            "FBTCV256151688S", "FBTCV246133765S", "FBTCV256152305S",
        }

    def test_address_with_no_commas(self, spider):
        rows = spider.parse_listing(_soup(FAIRFIELD_HTML), FAIRFIELD_URL)
        row = next(r for r in rows if r["id"] == "FBTCV256151688S")
        assert row["street"] == "736 Old Stratfield Road"
        assert row["city_state"] == "Fairfield, CT"

    def test_address_with_comma_and_zip(self, spider):
        rows = spider.parse_listing(_soup(FAIRFIELD_HTML), FAIRFIELD_URL)
        row = next(r for r in rows if r["id"] == "FBTCV246133765S")
        assert row["street"] == "325 Oldfield Road"
        assert row["city_state"] == "Fairfield, CT 06824"

    def test_town_name_repeated_in_street_does_not_confuse_split(self, spider):
        # "781 Fairfield Beach Road, Fairfield CT" -- "Fairfield" appears
        # twice; must split at the trailing occurrence only.
        rows = spider.parse_listing(_soup(FAIRFIELD_HTML), FAIRFIELD_URL)
        row = next(r for r in rows if r["id"] == "FBTCV256152305S")
        assert row["street"] == "781 Fairfield Beach Road"
        assert row["city_state"] == "Fairfield, CT"

    def test_datetime_gets_space_before_ampm(self, spider):
        rows = spider.parse_listing(_soup(FAIRFIELD_HTML), FAIRFIELD_URL)
        assert all(" AM" in r["date_time"] or " PM" in r["date_time"] for r in rows)

    def test_status_defaults_active(self, spider):
        rows = spider.parse_listing(_soup(FAIRFIELD_HTML), FAIRFIELD_URL)
        assert all(r["status"] == "active" for r in rows)

    def test_url_is_the_view_full_notice_link(self, spider):
        rows = spider.parse_listing(_soup(FAIRFIELD_HTML), FAIRFIELD_URL)
        row = next(r for r in rows if r["id"] == "FBTCV256151688S")
        assert row["url"] == (
            "https://sso.eservices.jud.ct.gov/foreclosures/Public/"
            "PendPostDetailPublic.aspx?PostingId=61252"
        )

    def test_no_table_returns_empty_list(self, spider):
        rows = spider.parse_listing(_soup("<div>no table here</div>"), FAIRFIELD_URL)
        assert rows == []


# ---- _normalize_datetime ------------------------------------------------

class TestNormalizeDatetime:
    def test_inserts_space_before_ampm(self):
        assert _normalize_datetime("08/15/2026 12:00PM") == "08/15/2026 12:00 PM"

    def test_already_spaced_is_unchanged(self):
        assert _normalize_datetime("08/15/2026 12:00 PM") == "08/15/2026 12:00 PM"

    def test_empty_string_returns_empty(self):
        assert _normalize_datetime("") == ""


# ---- _split_address ------------------------------------------------

class TestSplitAddress:
    def test_no_commas(self):
        street, city_state = _split_address("736 Old Stratfield Road Fairfield CT", "Fairfield")
        assert street == "736 Old Stratfield Road"
        assert city_state == "Fairfield, CT"

    def test_comma_and_zip(self):
        street, city_state = _split_address("325 Oldfield Road, Fairfield, CT 06824", "Fairfield")
        assert street == "325 Oldfield Road"
        assert city_state == "Fairfield, CT 06824"

    def test_town_substring_in_street_does_not_false_match_early(self):
        street, city_state = _split_address("781 Fairfield Beach Road, Fairfield CT", "Fairfield")
        assert street == "781 Fairfield Beach Road"
        assert city_state == "Fairfield, CT"

    def test_town_not_found_falls_back_to_whole_text(self):
        street, city_state = _split_address("5 Shore Rd, Mystic, CT 06355", "Fairfield")
        assert street == "5 Shore Rd, Mystic, CT 06355"
        assert city_state == ""

    def test_empty_input(self):
        assert _split_address("", "Fairfield") == ("", "")