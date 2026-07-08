"""
conftest.py — shared pytest fixtures for the AuctionScout pipeline tests.

Makes load_csv.py, export_json.py, and dedup_properties.py importable as
modules even though they're designed to also be run standalone as scripts —
pytest needs the parent directory on sys.path to find them.
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql")



@pytest.fixture
def db_path(tmp_path):
    """A path to a fresh, schema-initialized SQLite database, unique per test."""
    path = str(tmp_path / "test_auctionscout.db")
    conn = sqlite3.connect(path)
    conn.executescript(open(SCHEMA_PATH, encoding="utf-8").read())
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def sample_row():
    """One realistic CSV row as a dict, matching load_csv.py's expected columns."""
    return {
        "Name": "130 Forest Avenue, Cohasset, MA",
        "Latitude": "42.250627377688",
        "Longitude": "-70.827416062463",
        "Source": "sullivan",
        "State": "MA",
        "Timing": "This Week",
        "Description": (
            "Single Family Home on 4+ Acres | Property Type: Residential; "
            "Mortgage Ref: Norfolk Cty. in Bk 41130, Pg 265; Lot Size: 4.32 acres; "
            "Square Feet: 4,268 sf; # Bedrooms: 4; # Baths: 2; Year Built: 1981; "
            "County: Norfolk"
        ),
        "Auction Date/Time": "Wed. Jul. 8, 2026 at 11 am",
        "Status": "on",
        "PDF Links": "https://sullivan-auctioneers.com/public_docs/x.pdf",
        "URL": "https://sullivan-auctioneers.com/auction/?id=21007",
    }


def write_csv(path, rows):
    """Write a list of row-dicts to a CSV file at path, using load_csv.py's expected columns."""
    import csv
    fieldnames = ["Name", "Latitude", "Longitude", "Source", "State", "Timing",
                  "Description", "Auction Date/Time", "Status", "PDF Links", "URL"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)