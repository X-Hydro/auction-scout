"""
conftest.py — shared pytest fixtures for the AuctionScout pipeline tests.

Makes load_csv.py, export_json.py, and dedup_properties.py importable as
modules even though they're designed to also be run standalone as scripts —
pytest needs the parent directory on sys.path to find them.

Also puts spiders/ on sys.path, so spider test modules (e.g.
test_brockscott.py) can do `from brockscott import ...` the same way
run-scout.py's own sys.path.insert(0, .../spiders) lets it do
`from spiders.brockscott import BrockScottSpider`.
"""

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPIDERS_DIR = os.path.join(ROOT, "spiders")

for _path in (ROOT, SPIDERS_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import pytest

SCHEMA_PATH = os.path.join(ROOT, "schema.sql")



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
    """One realistic CSV row as a dict, matching load_csv.py's expected columns.

    This intentionally represents an OLDER/fallback data shape -- property
    specs embedded in Description, no dedicated columns -- because that's
    exactly what parse_description()'s regex fallback exists to handle
    (see its docstring: sources that haven't been updated to emit
    dedicated columns yet). Keep this as-is; it's what TestParseDescription
    tests against. For the CURRENT data shape (dedicated columns, trimmed
    Description), use sample_row_with_columns below instead.
    """
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


@pytest.fixture
def sample_row_with_columns():
    """A CURRENT-format CSV row: dedicated columns (Property Type,
    Bedrooms, Bathrooms, Sqft, Lot Size, Year Built, County, Municipality)
    populated directly by the spider, Description holding only genuine
    leftover facts -- matching what sullivan.py's _extract_property_details()
    (and its AI counterpart) actually produce today.

    Values here are DELIBERATELY different from what Description alone
    would parse to (if Description had those fields embedded, which it
    doesn't in real current output, but the point stands for any future
    regression): this fixture exists to prove resolve_property_details()
    genuinely PREFERS explicit columns, not just fills gaps that happen
    to agree. See row_with_conflicting_description for the strongest
    version of that proof, where both sources are present and disagree.
    """
    return {
        "Name": "130 Forest Avenue, Cohasset, MA",
        "Latitude": "42.250627377688",
        "Longitude": "-70.827416062463",
        "Source": "sullivan",
        "State": "MA",
        "County": "Norfolk",
        "Municipality": "Cohasset",
        "Timing": "This Week",
        "Property Type": "Residential",
        "Bedrooms": "4",
        "Bathrooms": "2",
        "Sqft": "4,268",
        "Lot Size": "4.32 acres",
        "Year Built": "1981",
        "Description": "Mortgage Ref: Norfolk Cty. in Bk 41130, Pg 265; Rooms: 8",
        "Auction Date/Time": "Wed. Jul. 8, 2026 at 11 am",
        "Status": "on",
        "PDF Links": "https://sullivan-auctioneers.com/public_docs/x.pdf",
        "URL": "https://sullivan-auctioneers.com/auction/?id=21007",
    }


@pytest.fixture
def row_with_conflicting_description():
    """Dedicated columns AND a Description that would parse to DIFFERENT
    values (stale/legacy-style embedded text alongside current columns --
    a plausible real scenario if a source only partially migrated, or a
    row briefly has both during a transition). Bedrooms column says 4;
    Description text says 5. If resolve_property_details() ever regresses
    to preferring Description over the explicit column, this is the test
    that catches it -- sample_row_with_columns alone couldn't, since its
    Description has nothing conflicting to prefer instead."""
    return {
        "Name": "130 Forest Avenue, Cohasset, MA",
        "Latitude": "42.250627377688",
        "Longitude": "-70.827416062463",
        "Source": "sullivan",
        "State": "MA",
        "County": "Norfolk",
        "Municipality": "Cohasset",
        "Timing": "This Week",
        "Property Type": "Residential",
        "Bedrooms": "4",
        "Bathrooms": "2",
        "Sqft": "4,268",
        "Lot Size": "4.32 acres",
        "Year Built": "1981",
        "Description": (
            "Property Type: Land; # Bedrooms: 5; # Baths: 3; "
            "Square Feet: 9,999 sf; Year Built: 1900; "
            "Mortgage Ref: Norfolk Cty. in Bk 41130, Pg 265"
        ),
        "Auction Date/Time": "Wed. Jul. 8, 2026 at 11 am",
        "Status": "on",
        "PDF Links": "https://sullivan-auctioneers.com/public_docs/x.pdf",
        "URL": "https://sullivan-auctioneers.com/auction/?id=21007",
    }


def write_csv(path, rows):
    """Write a list of row-dicts to a CSV file at path, using load_csv.py's expected columns."""
    import csv
    fieldnames = ["Name", "Latitude", "Longitude", "Source", "State", "County",
                  "Municipality", "Timing", "Property Type", "Bedrooms", "Bathrooms",
                  "Sqft", "Lot Size", "Year Built", "Description", "Auction Date/Time",
                  "Status", "PDF Links", "URL"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)