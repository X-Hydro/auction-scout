"""Tests for export_json.py — the map-export filtering logic."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import export_json


def insert_property_and_auction(db_path, *, source="sullivan", listing_id="1",
                                address="1 Test St, Boston, MA", lat=42.0, lon=-71.0,
                                status="on", last_seen_days_ago=0):
    """Directly inserts a minimal property+auction pair for controlled export tests."""
    ts = (datetime.now(timezone.utc) - timedelta(days=last_seen_days_ago)).isoformat()
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        """INSERT INTO properties
           (source, source_listing_id, address_raw, latitude, longitude, state,
            first_seen_at, last_seen_at)
           VALUES (?, ?, ?, ?, ?, 'MA', ?, ?)""",
        (source, listing_id, address, lat, lon, ts, ts),
    )
    property_id = cur.lastrowid
    conn.execute(
        """INSERT INTO auctions
           (property_id, auction_datetime_raw, auction_datetime, status,
            source_url, last_updated_at)
           VALUES (?, 'x', '2026-07-08T00:00:00', ?, 'https://x.example', ?)""",
        (property_id, status, ts),
    )
    conn.commit()
    conn.close()
    return property_id


class TestStatusFiltering:
    def test_live_status_is_exported(self, db_path, tmp_path):
        insert_property_and_auction(db_path, status="on")
        out = str(tmp_path / "out.json")
        export_json.export(db_path, out)
        data = json.load(open(out))
        assert data["count"] == 1

    def test_sold_status_is_excluded(self, db_path, tmp_path):
        insert_property_and_auction(db_path, status="sold back to mortgagee")
        out = str(tmp_path / "out.json")
        export_json.export(db_path, out)
        data = json.load(open(out))
        assert data["count"] == 0
        assert data["excluded_by_status"] == {"sold back to mortgagee": 1}

    def test_unrecognized_status_defaults_to_exported(self, db_path, tmp_path):
        """Blacklist approach: an unanticipated status string from a source
        we haven't seen before should default to LIVE, not silently dropped."""
        insert_property_and_auction(db_path, status="some new status text")
        out = str(tmp_path / "out.json")
        export_json.export(db_path, out)
        data = json.load(open(out))
        assert data["count"] == 1


class TestMissingCoordinates:
    def test_null_coordinates_excluded(self, db_path, tmp_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO properties
               (source, source_listing_id, address_raw, latitude, longitude,
                state, first_seen_at, last_seen_at)
               VALUES ('sullivan', '1', 'No Coords Ave', NULL, NULL, 'MA', 'x', 'x')"""
        )
        property_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            """INSERT INTO auctions
                   (property_id, status, source_url, last_updated_at)
               VALUES (?, 'on', 'https://x.example', 'x')""",
            (property_id,),
        )
        conn.commit()
        conn.close()

        out = str(tmp_path / "out.json")
        export_json.export(db_path, out)
        data = json.load(open(out))
        assert data["count"] == 0
        assert data["excluded_missing_coords_count"] == 1


class TestStaleness:
    def test_recently_seen_live_property_is_exported(self, db_path, tmp_path):
        insert_property_and_auction(db_path, status="on", last_seen_days_ago=1)
        out = str(tmp_path / "out.json")
        export_json.export(db_path, out)
        data = json.load(open(out))
        assert data["count"] == 1

    def test_stale_property_excluded_despite_live_status(self, db_path, tmp_path):
        insert_property_and_auction(db_path, status="on", last_seen_days_ago=30)
        out = str(tmp_path / "out.json")
        export_json.export(db_path, out)
        data = json.load(open(out))
        assert data["count"] == 0
        assert data["excluded_stale_count"] == 1

    def test_just_under_threshold_still_exported(self, db_path, tmp_path):
        insert_property_and_auction(db_path, status="on",
                                    last_seen_days_ago=export_json.STALE_AFTER_DAYS - 1)
        out = str(tmp_path / "out.json")
        export_json.export(db_path, out)
        data = json.load(open(out))
        assert data["count"] == 1


class TestDuplicateExclusion:
    def test_duplicate_link_hides_non_canonical(self, db_path, tmp_path):
        canonical_id = insert_property_and_auction(
            db_path, source="sullivan", listing_id="1", address="1 Canonical St, Boston, MA")
        dup_id = insert_property_and_auction(
            db_path, source="harmon_law", listing_id="2", address="1 Canonical St, Boston, MA")

        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO property_duplicate_links
               (property_id, canonical_property_id, match_distance_m, match_score, detected_at)
               VALUES (?, ?, 0, 1.0, 'x')""",
            (dup_id, canonical_id),
        )
        conn.commit()
        conn.close()

        out = str(tmp_path / "out.json")
        export_json.export(db_path, out)
        data = json.load(open(out))
        assert data["count"] == 1
        assert data["properties"][0]["property_id"] == canonical_id
        assert data["excluded_duplicates_count"] == 1

    def test_missing_dedup_table_degrades_gracefully(self, db_path, tmp_path):
        """If property_duplicate_links doesn't exist (older DB, schema.sql
        not yet re-run), export should still work rather than crash."""
        conn = sqlite3.connect(db_path)
        conn.execute("DROP TABLE property_duplicate_links")
        conn.commit()
        conn.close()

        insert_property_and_auction(db_path, status="on")
        out = str(tmp_path / "out.json")
        export_json.export(db_path, out)  # should not raise
        data = json.load(open(out))
        assert data["count"] == 1
        assert data["excluded_duplicates_count"] == 0


class TestMetaAccounting:
    def test_totals_add_up(self, db_path, tmp_path):
        insert_property_and_auction(db_path, source="sullivan", listing_id="1", status="on")
        insert_property_and_auction(db_path, source="sullivan", listing_id="2",
                                    status="sold back to mortgagee")
        insert_property_and_auction(db_path, source="sullivan", listing_id="3", status="on",
                                    last_seen_days_ago=30)

        out = str(tmp_path / "out.json")
        export_json.export(db_path, out)
        data = json.load(open(out))

        assert data["total_in_db"] == 3
        assert data["count"] == 1
        assert data["excluded_total"] == 2
        assert sum(data["excluded_by_status"].values()) + data["excluded_stale_count"] \
               == data["excluded_total"]