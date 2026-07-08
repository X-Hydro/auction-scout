"""Tests for load_csv.py — CSV parsing helpers and the full load() pipeline."""

import sqlite3

import load_csv
from conftest import write_csv


# ─────────────────────────────────────────────────────────────────────────
# Parsing helpers
# ─────────────────────────────────────────────────────────────────────────

class TestParseAuctionDatetime:
    def test_standard_format(self):
        result = load_csv.parse_auction_datetime("Wed. Jul. 8, 2026 at 11 am")
        assert result == "2026-07-08T11:00:00"

    def test_pm_time(self):
        result = load_csv.parse_auction_datetime("Thu. Jul. 16, 2026 at 2 pm")
        assert result.startswith("2026-07-16T14:00:00")

    def test_empty_string_returns_none(self):
        assert load_csv.parse_auction_datetime("") is None

    def test_garbage_input_returns_none_not_exception(self):
        assert load_csv.parse_auction_datetime("not a date at all !!!") is None


class TestParseDescription:
    def test_extracts_all_fields(self, sample_row):
        result = load_csv.parse_description(sample_row["Description"])
        assert result["property_type"] == "Residential"
        assert result["bedrooms"] == 4
        assert result["bathrooms"] == 2.0
        assert result["sqft"] == 4268
        assert result["year_built"] == 1981
        assert result["county"] == "Norfolk"
        assert "Norfolk Cty." in result["mortgage_ref"]

    def test_full_and_half_baths(self):
        desc = "# Baths: 3 full / 2 half; Year Built: 1915"
        result = load_csv.parse_description(desc)
        # 3 full + 2 half (0.5 each) = 4.0 total bath-equivalents
        assert result["bathrooms"] == 4.0

    def test_simple_bath_count(self):
        desc = "# Baths: 2; Year Built: 1970"
        result = load_csv.parse_description(desc)
        assert result["bathrooms"] == 2.0

    def test_missing_fields_are_none_not_crash(self):
        result = load_csv.parse_description("Multi-Family Home | Property Type: Residential")
        assert result["bedrooms"] is None
        assert result["county"] is None

    def test_empty_description(self):
        result = load_csv.parse_description("")
        assert result["property_type"] is None
        assert result["bedrooms"] is None


class TestExtractListingId:
    def test_extracts_id_param(self):
        url = "https://sullivan-auctioneers.com/auction/?id=21007"
        assert load_csv.extract_listing_id(url) == "21007"

    def test_falls_back_to_full_url_if_no_id(self):
        url = "https://example.com/listing/some-property"
        assert load_csv.extract_listing_id(url) == url


# ─────────────────────────────────────────────────────────────────────────
# Full load() pipeline
# ─────────────────────────────────────────────────────────────────────────

class TestLoadPipeline:
    def test_first_run_inserts_new_property_and_auction(self, db_path, tmp_path, sample_row):
        csv_path = str(tmp_path / "run1.csv")
        write_csv(csv_path, [sample_row])
        load_csv.load(csv_path, db_path)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        props = conn.execute("SELECT * FROM properties").fetchall()
        assert len(props) == 1
        assert props[0]["address_raw"] == "130 Forest Avenue, Cohasset, MA"

        events = conn.execute("SELECT * FROM auction_events").fetchall()
        assert len(events) == 1
        assert events[0]["event_type"] == "first_seen"
        conn.close()

    def test_rerun_with_no_changes_logs_nothing_new(self, db_path, tmp_path, sample_row):
        csv_path = str(tmp_path / "run.csv")
        write_csv(csv_path, [sample_row])
        load_csv.load(csv_path, db_path)
        load_csv.load(csv_path, db_path)  # identical second run

        conn = sqlite3.connect(db_path)
        # Still only one property, one auction, one event (first_seen only)
        assert conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM auction_events").fetchone()[0] == 1
        conn.close()

    def test_status_change_is_logged(self, db_path, tmp_path, sample_row):
        csv_path = str(tmp_path / "run.csv")
        write_csv(csv_path, [sample_row])
        load_csv.load(csv_path, db_path)

        changed = dict(sample_row)
        changed["Status"] = "postponed"
        csv_path2 = str(tmp_path / "run2.csv")
        write_csv(csv_path2, [changed])
        load_csv.load(csv_path2, db_path)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        events = conn.execute(
            "SELECT * FROM auction_events WHERE event_type='status_change'"
        ).fetchall()
        assert len(events) == 1
        assert events[0]["old_value"] == "on"
        assert events[0]["new_value"] == "postponed"
        conn.close()

    def test_date_change_is_logged(self, db_path, tmp_path, sample_row):
        csv_path = str(tmp_path / "run.csv")
        write_csv(csv_path, [sample_row])
        load_csv.load(csv_path, db_path)

        changed = dict(sample_row)
        changed["Auction Date/Time"] = "Wed. Jul. 15, 2026 at 11 am"
        csv_path2 = str(tmp_path / "run2.csv")
        write_csv(csv_path2, [changed])
        load_csv.load(csv_path2, db_path)

        conn = sqlite3.connect(db_path)
        events = conn.execute(
            "SELECT * FROM auction_events WHERE event_type='date_change'"
        ).fetchall()
        assert len(events) == 1
        conn.close()

    def test_malformed_row_does_not_abort_whole_run(self, db_path, tmp_path, sample_row):
        good_row = sample_row
        bad_row = dict(sample_row)
        bad_row["Latitude"] = "NOT_A_NUMBER"
        bad_row["URL"] = "https://sullivan-auctioneers.com/auction/?id=99999"

        csv_path = str(tmp_path / "run.csv")
        write_csv(csv_path, [good_row, bad_row])
        load_csv.load(csv_path, db_path)

        conn = sqlite3.connect(db_path)
        # The good row still loaded despite the bad one failing
        assert conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0] == 1
        conn.close()

    def test_disappeared_listing_logged_once_not_repeatedly(self, db_path, tmp_path, sample_row):
        other_row = dict(sample_row)
        other_row["URL"] = "https://sullivan-auctioneers.com/auction/?id=00001"
        other_row["Name"] = "1 Still Here St, Boston, MA"

        csv_path1 = str(tmp_path / "run1.csv")
        write_csv(csv_path1, [sample_row, other_row])
        load_csv.load(csv_path1, db_path)

        # sample_row's listing is gone, but 'sullivan' is still represented in
        # this run via other_row — so disappeared-detection can safely scope
        # to sullivan without guessing about sources it never saw at all.
        csv_path2 = str(tmp_path / "run2.csv")
        write_csv(csv_path2, [other_row])
        load_csv.load(csv_path2, db_path)
        load_csv.load(csv_path2, db_path)  # run again — should NOT double-log

        conn = sqlite3.connect(db_path)
        disappeared_events = conn.execute(
            "SELECT COUNT(*) FROM auction_events WHERE event_type='disappeared'"
        ).fetchone()[0]
        assert disappeared_events == 1
        conn.close()

    def test_completely_empty_csv_does_not_mark_everything_disappeared(self, db_path, tmp_path, sample_row):
        """A zero-row CSV can't distinguish 'this source genuinely has no live
        listings' from 'the scraper crashed and produced nothing' — so it
        must NOT trigger mass disappeared-marking. This is intentional
        conservative behavior, not a gap."""
        csv_path1 = str(tmp_path / "run1.csv")
        write_csv(csv_path1, [sample_row])
        load_csv.load(csv_path1, db_path)

        empty_csv = str(tmp_path / "run2.csv")
        write_csv(empty_csv, [])
        load_csv.load(empty_csv, db_path)

        conn = sqlite3.connect(db_path)
        disappeared_events = conn.execute(
            "SELECT COUNT(*) FROM auction_events WHERE event_type='disappeared'"
        ).fetchone()[0]
        assert disappeared_events == 0
        conn.close()

    def test_disappeared_only_scoped_to_sources_present_in_run(self, db_path, tmp_path, sample_row):
        """A property from a source NOT included in this run's CSV should not
        be marked disappeared — that would be wrong if only scraping one
        source's file at a time."""
        row_a = sample_row
        row_b = dict(sample_row)
        row_b["Source"] = "harmon_law"
        row_b["URL"] = "https://harmonlaw.example/?id=555"
        row_b["Name"] = "1 Other St, Boston, MA"

        csv_path1 = str(tmp_path / "run1.csv")
        write_csv(csv_path1, [row_a, row_b])
        load_csv.load(csv_path1, db_path)

        # Second run only includes sullivan — harmon_law wasn't scraped this time
        csv_path2 = str(tmp_path / "run2.csv")
        write_csv(csv_path2, [row_a])
        load_csv.load(csv_path2, db_path)

        conn = sqlite3.connect(db_path)
        disappeared = conn.execute(
            "SELECT COUNT(*) FROM auction_events WHERE event_type='disappeared'"
        ).fetchone()[0]
        assert disappeared == 0  # harmon_law shouldn't be flagged — it wasn't in this run at all
        conn.close()

    def test_schema_optional_when_db_already_initialized(self, db_path, tmp_path, sample_row, monkeypatch):
        """load() should work fine even without schema.sql present, as long
        as the target database already has the tables."""
        monkeypatch.setattr(load_csv, "SCRIPT_DIR", str(tmp_path))  # schema.sql won't be found here
        csv_path = str(tmp_path / "run.csv")
        write_csv(csv_path, [sample_row])
        load_csv.load(csv_path, db_path)  # db_path fixture already has tables

        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0] == 1
        conn.close()