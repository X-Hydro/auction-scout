"""
Shared cross-source dedup logic for the auction-scout pipeline.

Extracted from run-scout.py so the SAME normalization/matching code can be
used both when writing markers.csv AND when loading rows into the
persistent `properties` table in auctionscout.db. Previously this logic
only lived in run-scout.py, so the CSV output was deduped but the SQLite
`properties` table was not -- that's why harmon/patriot rows like
"65 Cecelia Terrace" ended up as two separate property_ids even though
run-scout.py's dedup_cross_source() would have collapsed them for the map.

DEDUP_SOURCE_PRIORITY is intentionally scoped to CONFIRMED overlapping
pairs only -- see run-scout.py's original comment. Do not add a pair here
based on a single coincidental address match; confirm it's a systemic
overlap first (same property listed independently by two sources), same
as the landmark/brockscott and harmon/patriot entries below.
"""

import re

# Confirmed overlaps:
#   - landmark / brockscott: 2026-07-30 (see run-scout.py history)
#   - harmon / patriot: 2026-08 -- e.g. "65 Cecelia Terrace, Pittsfield, MA"
#     and "59 Denver Street, Fall River, MA" both listed independently by
#     both sources with identical geocoded coordinates. Explains why they
#     overlap at all: Harmon Law owns Patriot, so Patriot listings are
#     largely a subset/mirror of Harmon's own auctions.
#   - brockscott / patriot: 2026-08 -- 19 same-coordinate matches surfaced
#     by migrate_properties_dedup.py's skipped-groups output in one run,
#     clearly systemic rather than coincidental.
#
# Priority = which source's row wins when both have a row for the same
# coordinates. harmon wins the harmon/patriot pair because Harmon Law
# owns Patriot -- the primary source, not a data-completeness judgment
# (an earlier version of this comment guessed at completeness; don't
# reintroduce that reasoning without actually re-measuring it). patriot
# wins the brockscott/patriot pair.

DEDUP_SOURCE_PRIORITY = {
    frozenset({"landmark", "brockscott"}): ["landmark", "brockscott"],
    frozenset({"brockscott", "patriot"}): ["patriot", "brockscott"],
    frozenset({"sullivan", "brockscott"}): ["sullivan", "brockscott"],
    frozenset({"harmon", "patriot"}): ["harmon", "patriot"],
}

_STREET_ABBR = {
    "STREET": "ST", "DRIVE": "DR", "ROAD": "RD", "AVENUE": "AVE",
    "LANE": "LN", "TERRACE": "TER", "CIRCLE": "CIR", "COURT": "CT",
    "PLACE": "PL", "BOULEVARD": "BLVD", "PARKWAY": "PKWY", "SQUARE": "SQ",
    "TRAIL": "TRL", "EXTENSION": "EXT", "HIGHWAY": "HWY",
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
}
_UNIT_RE = re.compile(r"\b(?:UNIT|APT|#)\s*([A-Z0-9-]+)\b", re.IGNORECASE)
_ZIP_RE = re.compile(r"(\d{5})(?:-\d{4})?\s*$")


def normalize_street(street):
    """Uppercase, strip punctuation, and normalize common street-type/
    directional words so '65 Cecelia Terrace' and '65 Cecelia Ter' (or a
    trailing 'MA 01201' zip variant) compare equal. Only compares against
    the FIRST name when a title has an 'a/k/a' alias."""
    if not street:
        return ""
    s = street.split(" a/k/a ")[0]
    s = re.sub(r"[.,]", "", s).upper().strip()
    s = re.sub(r"\s+", " ", s)
    for full, abbr in _STREET_ABBR.items():
        s = re.sub(rf"\b{full}\b", abbr, s)
    s = _UNIT_RE.sub("", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s


def dedup_key(street, city_state):
    """(zip, normalized street, unit) -- unit is kept SEPARATE from the
    street so two different units in the same building are never treated
    as duplicates of each other. Missing zip (city_state has none) ->
    empty zip_code, which callers should treat as "can't confidently
    match" rather than silently matching on street alone.

    Works directly off (street, city_state) so it applies equally to a
    CSV-shaped row dict and to an `address_raw`-shaped DB row (split
    address_raw on the first comma to get street / city_state)."""
    zip_match = _ZIP_RE.search(city_state or "")
    zip_code = zip_match.group(1) if zip_match else ""
    unit_match = _UNIT_RE.search(street or "")
    unit = unit_match.group(1).upper() if unit_match else ""
    return zip_code, normalize_street(street), unit


def coord_key(lat, lon, precision=6):
    """Rounded (lat, lon) pair for matching cross-source duplicates AFTER
    geocoding (e.g. in load_csv.py) -- much more reliable than
    normalize_street()/dedup_key() for this stage, since two rows for the
    truly same address are independently geocoded to (near-)identical
    coordinates regardless of how differently their source sites format
    the address text (missing zip, "Fall RIver" vs "Fall River" typos,
    etc. -- both break string matching but not this).

    precision=6 decimal places is ~11cm -- tight enough that two distinct
    (even adjacent) properties essentially never collide, loose enough to
    absorb float noise. Only call this on rows that already have real
    coordinates: rows that failed geocoding should never reach this point
    (see run-scout.py's write_csv, which skips latitude=None rows), so
    there's no "unkeyable" case to handle here the way dedup_key() has
    for missing zips."""
    return (round(float(lat), precision), round(float(lon), precision))


def winning_source(sources_present):
    """Given a set of source names that all produced a row for the same
    dedup_key, return the priority-ordered list to sort by, or None if
    this isn't a confirmed overlapping pair (caller should keep every
    row rather than guess)."""
    return next(
        (order for pair, order in DEDUP_SOURCE_PRIORITY.items()
         if sources_present <= pair),
        None,
    )