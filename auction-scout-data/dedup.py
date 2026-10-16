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

import math
import re


# Priority = which source's row wins when both have a row for the same
# coordinates. harmon wins the harmon/patriot pair because Harmon Law
# owns Patriot -- the primary source, not a data-completeness judgment
# (an earlier version of this comment guessed at completeness; don't
# reintroduce that reasoning without actually re-measuring it). patriot
# wins the brockscott/patriot pair. ct_judicial wins its pair because it's
# the court's own record, and brockscott rows carry no photos/terms.

DEDUP_SOURCE_PRIORITY = {
    frozenset({"landmark", "brockscott"}): ["landmark", "brockscott"],
    frozenset({"brockscott", "patriot"}): ["patriot", "brockscott"],
    frozenset({"sullivan", "brockscott"}): ["sullivan", "brockscott"],
    frozenset({"harmon", "patriot"}): ["harmon", "patriot"],
    frozenset({"ct_judicial", "brockscott"}): ["ct_judicial", "brockscott"],
    frozenset({"sullivan", "harmon"}): ["sullivan", "harmon"],
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

def coord_key(lat, lon, precision=5):
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


# Max distance between two geocoded points still treated as the same
# property. Rounding to N decimals (coord_key) can't do this job: two
# points 0.22 m apart can straddle a rounding boundary at ANY precision --
# confirmed 2026-10-16 with "100 Wheelock Rd, Sutton, MA" (brockscott
# 42.162716732728,-71.765976591887 vs patriot 42.16271806473,
# -71.76597458466: different keys at precision 5 AND 6). Same-address
# geocodes differ by centimetres; distinct buildings are metres apart,
# so 2 m separates the two cases with room on both sides.
COORD_MATCH_TOLERANCE_M = 2.0

_EARTH_RADIUS_M = 6_371_000
_M_PER_DEG_LAT = 111_320


def _distance_m(a, b):
    """Equirectangular distance in metres -- exact enough at the few-metre
    scale this is used for, and far cheaper than haversine."""
    (lat1, lon1), (lat2, lon2) = a, b
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return _EARTH_RADIUS_M * math.hypot(x, y)


def cluster_by_distance(items, coords_of, tolerance_m=COORD_MATCH_TOLERANCE_M):
    """Group items whose coordinates are within tolerance_m of each other
    (transitively -- A~B and B~C puts A, B, C in one group). coords_of(item)
    -> (lat, lon) floats. Returns a list of groups, each preserving the
    input order of its items. Replaces grouping by coord_key() in
    load_csv.py; see COORD_MATCH_TOLERANCE_M for why."""
    pts = [coords_of(it) for it in items]
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    # Sweep in latitude order so each point is only compared with the few
    # neighbours inside the tolerance band, not every other row.
    order = sorted(range(len(items)), key=lambda i: pts[i][0])
    band = tolerance_m / _M_PER_DEG_LAT
    for n, i in enumerate(order):
        for j in order[n + 1:]:
            if pts[j][0] - pts[i][0] > band:
                break
            if _distance_m(pts[i], pts[j]) <= tolerance_m:
                parent[find(i)] = find(j)

    groups = {}
    for i, item in enumerate(items):
        groups.setdefault(find(i), []).append(item)
    return list(groups.values())


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