"""Tests for dedup_properties.py — cross-source duplicate detection."""

import sqlite3

import dedup_properties


class TestParseHouseStreet:
    def test_basic_address(self):
        house, street = dedup_properties.parse_house_street("130 Forest Avenue, Cohasset, MA")
        assert house == "130"
        assert street == "forest ave"  # 'avenue' normalized to 'ave'

    def test_abbreviation_normalization(self):
        _, street1 = dedup_properties.parse_house_street("11 Lawson Drive, Easthampton, MA")
        _, street2 = dedup_properties.parse_house_street("11 Lawson Dr, Easthampton MA")
        assert street1 == street2

    def test_no_leading_number_returns_none(self):
        house, street = dedup_properties.parse_house_street("Forest Avenue, Cohasset, MA")
        assert house is None

    def test_house_number_with_letter_suffix(self):
        house, _ = dedup_properties.parse_house_street("130A Forest Avenue, Cohasset, MA")
        assert house == "130a"


class TestHaversine:
    def test_zero_distance_for_identical_points(self):
        d = dedup_properties.haversine_m(42.25, -70.83, 42.25, -70.83)
        assert d == 0

    def test_known_short_distance_is_reasonable(self):
        # Two points ~111m apart in latitude (1 arc-minute of latitude ≈ 1852m,
        # so 0.001 degrees ≈ 111m) — sanity check the formula isn't wildly off.
        d = dedup_properties.haversine_m(42.250000, -70.830000, 42.251000, -70.830000)
        assert 100 < d < 120


class TestChooseCanonical:
    def make_prop(self, property_id, status, last_updated_at):
        return {"property_id": property_id, "status": status, "last_updated_at": last_updated_at}

    def test_live_beats_non_live(self):
        live = self.make_prop(1, "on", "2026-01-01")
        sold = self.make_prop(2, "sold back to mortgagee", "2026-06-01")  # even though newer
        canonical, duplicate = dedup_properties.choose_canonical(live, sold)
        assert canonical["property_id"] == 1
        assert duplicate["property_id"] == 2

    def test_more_recent_wins_when_both_live(self):
        older = self.make_prop(1, "on", "2026-01-01")
        newer = self.make_prop(2, "on", "2026-06-01")
        canonical, duplicate = dedup_properties.choose_canonical(older, newer)
        assert canonical["property_id"] == 2

    def test_lower_id_tiebreak_when_fully_tied(self):
        a = self.make_prop(5, "on", "2026-01-01")
        b = self.make_prop(3, "on", "2026-01-01")
        canonical, duplicate = dedup_properties.choose_canonical(a, b)
        assert canonical["property_id"] == 3


class TestFullDedupRun:
    def insert(self, db_path, source, listing_id, address, lat, lon, status="on",
               last_updated_at="2026-07-01T00:00:00"):
        conn = sqlite3.connect(db_path)
        cur = conn.execute(
            """INSERT INTO properties
               (source, source_listing_id, address_raw, latitude, longitude, state,
                first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, 'MA', 'x', 'x')""",
            (source, listing_id, address, lat, lon),
        )
        property_id = cur.lastrowid
        conn.execute(
            """INSERT INTO auctions
                   (property_id, status, source_url, last_updated_at)
               VALUES (?, ?, 'https://x.example', ?)""",
            (property_id, status, last_updated_at),
        )
        conn.commit()
        conn.close()
        return property_id

    def test_true_cross_source_duplicate_detected(self, db_path):
        self.insert(db_path, "sullivan", "1", "130 Forest Avenue, Cohasset, MA", 42.2506, -70.8274)
        self.insert(db_path, "harmon_law", "2", "130 Forest Ave, Cohasset MA", 42.2506, -70.8274)

        dedup_properties.dedup(db_path)

        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM property_duplicate_links").fetchone()[0]
        assert count == 1
        conn.close()

    def test_adjacent_different_houses_not_merged(self, db_path):
        """The core false-positive guard: close together, similar street name,
        but DIFFERENT house numbers — must NOT be flagged as duplicates."""
        self.insert(db_path, "sullivan", "1", "126 North Street, East Brookfield, MA", 42.2281, -72.0548)
        self.insert(db_path, "harmon_law", "2", "128 North Street, East Brookfield, MA", 42.2281, -72.0548)

        dedup_properties.dedup(db_path)

        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM property_duplicate_links").fetchone()[0]
        assert count == 0
        conn.close()

    def test_same_source_pairs_are_never_compared(self, db_path):
        """load_csv.py's own (source, listing_id) key already prevents same-source
        dupes — dedup_properties.py should never even consider same-source pairs,
        even if they happen to share identical address/coords by data error."""
        self.insert(db_path, "sullivan", "1", "1 Test St, Boston, MA", 42.0, -71.0)
        self.insert(db_path, "sullivan", "2", "1 Test St, Boston, MA", 42.0, -71.0)

        dedup_properties.dedup(db_path)

        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM property_duplicate_links").fetchone()[0]
        assert count == 0
        conn.close()

    def test_far_apart_properties_not_merged(self, db_path):
        self.insert(db_path, "sullivan", "1", "1 Test St, Boston, MA", 42.0, -71.0)
        self.insert(db_path, "harmon_law", "2", "1 Test St, Springfield, MA", 42.5, -72.5)

        dedup_properties.dedup(db_path)

        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM property_duplicate_links").fetchone()[0]
        assert count == 0
        conn.close()

    def test_rerun_is_idempotent(self, db_path):
        self.insert(db_path, "sullivan", "1", "130 Forest Avenue, Cohasset, MA", 42.2506, -70.8274)
        self.insert(db_path, "harmon_law", "2", "130 Forest Ave, Cohasset MA", 42.2506, -70.8274)

        dedup_properties.dedup(db_path)
        dedup_properties.dedup(db_path)  # run again — should not duplicate the link row

        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM property_duplicate_links").fetchone()[0]
        assert count == 1
        conn.close()

    def test_live_listing_preferred_as_canonical(self, db_path):
        sold_id = self.insert(db_path, "harmon_law", "1", "130 Forest Avenue, Cohasset, MA",
                              42.2506, -70.8274, status="sold back to mortgagee")
        live_id = self.insert(db_path, "sullivan", "2", "130 Forest Ave, Cohasset MA",
                              42.2506, -70.8274, status="on")

        dedup_properties.dedup(db_path)

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT property_id, canonical_property_id FROM property_duplicate_links"
        ).fetchone()
        hidden_id, canonical_id = row
        assert canonical_id == live_id
        assert hidden_id == sold_id
        conn.close()