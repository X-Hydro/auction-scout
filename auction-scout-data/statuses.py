"""
Shared status-value constants for the AuctionScout scrape/load/export pipeline.

Single source of truth for which raw `auctions.status` values mean "this
auction is over" -- used by load_csv.py (to detect and log a past-due
auction that no source ever explicitly flagged) and export_json.py (to
keep those same statuses off the live map). Keeping this in one place
means a new terminal status only needs to be added once, rather than kept
in sync by hand across two separate scripts.
"""

# Explicit terminal-status wording seen across sources so far -- each
# source's own way of saying an auction already concluded with a sale or
# was called off. Add to this list as new terminal-status wording turns
# up across sources (see export_json.py's module docstring for the "why a
# blocklist and not an allowlist" rationale).
EXPLICIT_TERMINAL_STATUSES = (
    "sold back to mortgagee",
    "3rd party purchase",
    "sold",
    "canceled",
    "cancelled",
    "withdrawn",
    "bank buy back",
)

# Status load_csv.py assigns itself when an auction's date has passed with
# no explicit terminal status ever reported by the source -- i.e. the
# source still shows it "active"/"on_time"/whatever its own still-scheduled
# wording is, but the date has come and gone. Distinct from the statuses
# above because no source ever said this; it's an inference load_csv.py
# makes locally based on the clock, not something scraped from a page.
PAST_DUE_STATUS = "completed"

# Everything that should be excluded from the live map export -- both
# statuses a source told us about directly, and the one load_csv.py infers.
EXCLUDED_STATUSES = EXPLICIT_TERMINAL_STATUSES + (PAST_DUE_STATUS,)

# The single canonical value load_csv.py writes to auctions.status for any
# raw source status that ISN'T explicitly terminal -- collapsing "on_time",
# "live", "scheduled", or whatever else a given site happens to call a
# still-upcoming auction into one value every consumer (frontend, digest
# emails, generate_fb_posts.py, ad-hoc SQL) can rely on. "active" rather
# than "scheduled" deliberately -- it's already the dominant raw wording
# across sources (the vast majority of live rows), so this is the value
# other parts of the app (frontend, admin dashboard) are most likely to
# already assume, and it requires the least actual change to the data.
ACTIVE_STATUS = "active"


def normalize_status(raw_status: str) -> str:
    """
    Collapse a source's free-text "still on" wording into ACTIVE_STATUS.

    Deliberately derived from EXCLUDED_STATUSES rather than a hand-maintained
    list of "live" synonyms ("active", "on_time", ...) -- an allowlist like
    that has to anticipate every future source's wording in advance, and a
    source using an unanticipated word silently falls through the cracks
    (see: the Landmark "active" bug this was written to fix). Terminal
    wording is a small, closed, and individually meaningful vocabulary
    (sold vs. cancelled vs. withdrawn), so it's cheap to enumerate and worth
    keeping distinct -- that's what EXCLUDED_STATUSES already does. Anything
    NOT in it is, by definition, still on, so it's safe to collapse.

    The raw text itself isn't lost -- see auctions.status_raw, which
    load_csv.py stores alongside this normalized value.
    """
    raw_status = (raw_status or "").strip().lower()
    if raw_status in EXCLUDED_STATUSES:
        return raw_status
    return ACTIVE_STATUS