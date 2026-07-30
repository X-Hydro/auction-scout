"""
Tests for landmark.py -- _parse_address(), _format_date_chunk(),
_parse_continued_date(), the status-branching inside _parse_event(), and
_fallback_containers()'s DOM-climbing logic.

LandmarkSpider()'s __init__ makes a live robots.txt request (inherited
from AuctionSpider). As in test_harmon.py/test_sullivan.py, we bypass it
with __new__ for these pure-parsing tests -- none of the methods tested
here touch anything __init__ sets up.

landmark.py is an unverified DRAFT (see its module docstring) -- these
tests lock in the INTENDED behavior against synthetic HTML built to match
ASSUMPTION #1 (article.eventlist-event). If the real site's markup turns
out to differ, update the synthetic HTML here to match reality, not the
other way around -- these tests can't confirm the assumption itself, only
that the parsing logic does what it's supposed to given that shape.
"""

from datetime import datetime

import pytest
from bs4 import BeautifulSoup

from base import DATE_CHUNK_RE
from landmark import (
    LandmarkSpider,
    _format_date_chunk,
    _parse_address,
    _parse_continued_date,
)


@pytest.fixture
def spider():
    return LandmarkSpider.__new__(LandmarkSpider)


def _soup(html):
    return BeautifulSoup(html, "html.parser")


# ---- _parse_address ------------------------------------------------

class TestParseAddress:
    def test_simple_address(self):
        street, city_state, note = _parse_address("46 George St, Plainville, MA 02762")
        assert street == "46 George St"
        assert city_state == "Plainville, MA 02762"
        assert note == ""

    def test_trailing_note_after_zip_not_folded_into_city(self):
        street, city_state, note = _parse_address(
            "130 Rachael Ter, Westfield, MA 01085 (2ND MORTGAGE)"
        )
        assert street == "130 Rachael Ter"
        assert city_state == "Westfield, MA 01085"
        assert note == "2ND MORTGAGE"

    def test_internal_comma_in_street_half(self):
        # "Apt. 510 a/k/a Unit 510" must stay part of the street, not get
        # mistaken for the city.
        street, city_state, note = _parse_address(
            "111 Foster St, Apt. 510 a/k/a Unit 510, Peabody, MA 01960"
        )
        assert street == "111 Foster St, Apt. 510 a/k/a Unit 510"
        assert city_state == "Peabody, MA 01960"
        assert note == ""

    def test_parenthetical_embedded_in_city_not_treated_as_trailing_note(self):
        # "(Boston)" sits INSIDE the city segment here (before state/zip),
        # unlike the 2ND MORTGAGE case where the note comes after the zip.
        street, city_state, note = _parse_address(
            "315 Allston St, Unit 11 a/k/a Unit 315-11, Brighton (Boston), MA 02135"
        )
        assert street == "315 Allston St, Unit 11 a/k/a Unit 315-11"
        assert city_state == "Brighton (Boston), MA 02135"
        assert note == ""

    def test_no_city_state_zip_match_returns_whole_title_as_street(self):
        street, city_state, note = _parse_address("Not a real address at all")
        assert street == "Not a real address at all"
        assert city_state == ""
        assert note == ""

    def test_state_abbreviation_is_uppercased(self):
        _, city_state, _ = _parse_address("1 Main St, Somewhere, ma 05341")
        assert city_state == "Somewhere, MA 05341"


# ---- _format_date_chunk ---------------------------------------------

class TestFormatDateChunk:
    def test_none_returns_empty_string(self):
        assert _format_date_chunk(None) == ""

    def test_formats_into_month_day_year_at_time_shape(self):
        dt = datetime(2026, 10, 1, 16, 0)
        assert _format_date_chunk(dt) == "October 01, 2026 at 04:00 PM"

    def test_output_is_parseable_by_base_pys_shared_date_chunk_regex(self):
        # This is the regression test for the bug found when base.py was
        # reviewed: base.py's scrape() unconditionally re-parses
        # row["date_time"] via DATE_CHUNK_RE, with no rollover logic of
        # its own. _format_date_chunk()'s whole job is to hand back text
        # that regex can match unambiguously (year included) -- if this
        # ever stops matching, base.py silently falls back to
        # dateutil's raw fuzzy-parse-the-whole-string path instead of the
        # single clean chunk, and rollover-sensitive years (see
        # _parse_continued_date) stop being trustworthy downstream.
        dt = datetime(2027, 1, 5, 9, 0)
        chunk = _format_date_chunk(dt)
        match = DATE_CHUNK_RE.search(chunk)
        assert match is not None
        assert match.group(0) == chunk


# ---- _parse_continued_date --------------------------------------------

class TestParseContinuedDate:
    def test_forward_date_same_year_as_reference_no_rollover(self):
        reference = datetime(2026, 7, 28)
        dt = _parse_continued_date("October 1 at 4 PM", reference=reference)
        assert dt.year == 2026
        assert dt.month == 10
        assert dt.day == 1
        assert dt.hour == 16

    def test_date_before_reference_minus_30_days_rolls_to_next_year(self):
        # A continuance to "January 5" referenced against a December date
        # means next year, not a January that's already 11 months past.
        reference = datetime(2026, 12, 1)
        dt = _parse_continued_date("January 5 at 2 PM", reference=reference)
        assert dt.year == 2027
        assert dt.month == 1
        assert dt.day == 5

    def test_empty_text_returns_none(self):
        assert _parse_continued_date("", reference=datetime(2026, 7, 28)) is None

    def test_none_text_returns_none(self):
        assert _parse_continued_date(None, reference=datetime(2026, 7, 28)) is None

    def test_unparsable_text_returns_none_not_crash(self):
        assert _parse_continued_date("not a date", reference=datetime(2026, 7, 28)) is None

    def test_missing_reference_defaults_to_now_without_crashing(self):
        # Just confirms the no-reference path is exercised safely -- the
        # exact resolved year depends on when the test runs.
        dt = _parse_continued_date("October 1 at 4 PM")
        assert dt is not None


# ---- _parse_event / parse_listing: status branching (end-to-end) ------

def _event_html(
        href="/auction-calendar/2026/7/30/46-george-st-plainville-ma-02762",
        title="46 George St, Plainville, MA 02762",
        date_line="Thursday, July 30, 2026",
        time_line="10:00 AM 11:00 AM 10:00 11:00",
        status_line="Currently going forward",
        registry_line="Book 22019 at Page 373",
):
    return f"""
    <article class="eventlist-event">
      <h1><a href="{href}">{title}</a></h1>
      <ul>
        <li>{date_line}</li>
        <li>{time_line}</li>
      </ul>
      <div>{status_line}</div>
      <div>{registry_line}</div>
    </article>
    """


class TestParseListingStatusBranches:
    def test_active_listing(self, spider):
        html = _event_html()
        rows = spider.parse_listing(_soup(html), "https://www.landmarkauction.biz/")
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "active"
        assert row["date_time"] == "July 30, 2026 at 10:00 AM"
        assert row["street"] == "46 George St"
        assert row["city_state"] == "Plainville, MA 02762"
        assert row["id"] == "2026/7/30/46-george-st-plainville-ma-02762"
        assert "Book/Page: 22019/373" in row["extra_fields"]

    def test_canceled_listing(self, spider):
        html = _event_html(
            href="/auction-calendar/2026/7/30/184-grayson-dr-springfield-ma-01119",
            title="184 Grayson Dr, Springfield, MA 01119",
            status_line="CANCELED",
            registry_line="Book 16405 at Page 345",
        )
        rows = spider.parse_listing(_soup(html), "https://www.landmarkauction.biz/")
        assert rows[0]["status"] == "canceled"
        assert rows[0]["date_time"] == "July 30, 2026 at 10:00 AM"

    def test_double_l_cancelled_spelling_also_matches(self, spider):
        html = _event_html(status_line="CANCELLED")
        rows = spider.parse_listing(_soup(html), "https://www.landmarkauction.biz/")
        assert rows[0]["status"] == "canceled"

    def test_continued_listing_resolves_year_and_records_original_date(self, spider):
        html = _event_html(
            href="/auction-calendar/2026/7/28/12-sunnyside-dr-durham-nh-03824",
            title="12 Sunnyside Dr, Durham, NH 03824",
            date_line="Tuesday, July 28, 2026",
            time_line="3:00 PM 4:00 PM 15:00 16:00",
            status_line="Continued to August 28 at 4 PM",
            registry_line="",
        )
        rows = spider.parse_listing(_soup(html), "https://www.landmarkauction.biz/")
        row = rows[0]
        assert row["status"] == "postponed"
        assert row["date_time"] == "August 28, 2026 at 04:00 PM"
        assert "Originally scheduled: Tuesday, July 28, 2026" in row["extra_fields"]

    def test_continued_listing_rolls_year_over_boundary(self, spider):
        html = _event_html(
            date_line="Wednesday, December 2, 2026",
            time_line="1:00 PM 2:00 PM 13:00 14:00",
            status_line="Continued to January 5 at 2 PM",
        )
        rows = spider.parse_listing(_soup(html), "https://www.landmarkauction.biz/")
        assert rows[0]["date_time"] == "January 05, 2027 at 02:00 PM"

    def test_doc_cert_registry_used_when_no_book_page(self, spider):
        html = _event_html(
            title="315 Allston St, Unit 11 a/k/a Unit 315-11, Brighton (Boston), MA 02135",
            registry_line="Doc #816141, Cert of Title #C99-693",
        )
        rows = spider.parse_listing(_soup(html), "https://www.landmarkauction.biz/")
        extra = rows[0]["extra_fields"]
        assert "Doc #: 816141" in extra
        assert "Cert of Title #: C99-693" in extra
        assert rows[0]["street"] == "315 Allston St, Unit 11 a/k/a Unit 315-11"
        assert rows[0]["city_state"] == "Brighton (Boston), MA 02135"

    def test_trailing_note_lands_in_extra_fields(self, spider):
        html = _event_html(
            href="/auction-calendar/2026/7/30/130-rachael-ter-westfield-ma-01085-2nd-mortgage",
            title="130 Rachael Ter, Westfield, MA 01085 (2ND MORTGAGE)",
        )
        rows = spider.parse_listing(_soup(html), "https://www.landmarkauction.biz/")
        assert "2ND MORTGAGE" in rows[0]["extra_fields"]

    def test_no_registry_line_present_no_crash(self, spider):
        html = _event_html(registry_line="")
        rows = spider.parse_listing(_soup(html), "https://www.landmarkauction.biz/")
        assert rows[0]["extra_fields"] == ""

    def test_missing_h1_is_skipped(self, spider):
        html = """
        <article class="eventlist-event">
          <div>no h1 in here</div>
        </article>
        """
        rows = spider.parse_listing(_soup(html), "https://www.landmarkauction.biz/")
        assert rows == []

    def test_multiple_listings_all_parsed_with_unique_ids(self, spider):
        html = _event_html() + _event_html(
            href="/auction-calendar/2026/8/6/3-b-st-pembroke-ma-02359",
            title="3 B St, Pembroke, MA 02359",
            registry_line="Book 39678 at Page 219",
        )
        rows = spider.parse_listing(_soup(html), "https://www.landmarkauction.biz/")
        assert len(rows) == 2
        assert {r["id"] for r in rows} == {
            "2026/7/30/46-george-st-plainville-ma-02762",
            "2026/8/6/3-b-st-pembroke-ma-02359",
        }

    def test_duplicate_href_within_same_scrape_not_double_counted(self, spider):
        # Contrived (shouldn't happen on the real site since URLs are
        # inherently unique per event) but parse_listing's seen_ids guard
        # should still hold if it ever does.
        single = _event_html()
        html = single + single
        rows = spider.parse_listing(_soup(html), "https://www.landmarkauction.biz/")
        assert len(rows) == 1

    def test_pdf_links_defensively_empty_when_none_present(self, spider):
        html = _event_html()
        rows = spider.parse_listing(_soup(html), "https://www.landmarkauction.biz/")
        assert rows[0]["pdf_links"] == ""

    def test_pdf_link_is_picked_up_if_present(self, spider):
        html = """
        <article class="eventlist-event">
          <h1><a href="/auction-calendar/2026/7/30/46-george-st-plainville-ma-02762">46 George St, Plainville, MA 02762</a></h1>
          <ul><li>Thursday, July 30, 2026</li><li>10:00 AM 11:00 AM 10:00 11:00</li></ul>
          <div>Currently going forward</div>
          <a href="/docs/Notice of Sale.pdf">Notice</a>
        </article>
        """
        rows = spider.parse_listing(_soup(html), "https://www.landmarkauction.biz/")
        assert "Notice%20of%20Sale.pdf" in rows[0]["pdf_links"]


# ---- primary selector vs fallback container discovery -----------------

class TestContainerDiscovery:
    def test_primary_eventlist_selector_used_when_present(self, spider):
        html = _event_html()
        rows = spider.parse_listing(_soup(html), "https://www.landmarkauction.biz/")
        assert len(rows) == 1

    def test_falls_back_when_eventlist_class_absent(self, spider):
        # Anchor WRAPS the h1 here (opposite nesting from _event_html's
        # h1-wraps-anchor shape) -- exercises the h1.find_parent("a", ...)
        # branch in _parse_event as well as the climb-to-signature logic
        # in _fallback_containers.
        html = """
        <div class="some-other-wrapper">
          <a href="/auction-calendar/2026/7/30/9-east-st-ayer-ma-01432">
            <h1>9 East St, Ayer, MA 01432</h1>
          </a>
          <p>Thursday, July 30, 2026</p>
          <p>3:00 PM 4:00 PM 15:00 16:00</p>
          <p>Currently going forward</p>
          <p>Book 78439 at Page 282</p>
        </div>
        """
        rows = spider.parse_listing(_soup(html), "https://www.landmarkauction.biz/")
        assert len(rows) == 1
        assert rows[0]["street"] == "9 East St"
        assert rows[0]["city_state"] == "Ayer, MA 01432"
        assert rows[0]["status"] == "active"

    def test_fallback_prints_warning(self, spider, capsys):
        html = """
        <div>
          <a href="/auction-calendar/2026/7/30/9-east-st-ayer-ma-01432">
            <h1>9 East St, Ayer, MA 01432</h1>
          </a>
          <p>Thursday, July 30, 2026</p>
          <p>3:00 PM 4:00 PM 15:00 16:00</p>
          <p>Currently going forward</p>
        </div>
        """
        spider.parse_listing(_soup(html), "https://www.landmarkauction.biz/")
        captured = capsys.readouterr()
        assert "falling back to signature-based grouping" in captured.out

    def test_no_event_links_at_all_returns_empty_list(self, spider):
        rows = spider.parse_listing(_soup("<div>nothing here</div>"), "https://www.landmarkauction.biz/")
        assert rows == []