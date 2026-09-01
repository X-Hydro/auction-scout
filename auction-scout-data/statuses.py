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