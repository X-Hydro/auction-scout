#!/usr/bin/env bash
#
# mark_canceled.sh -- manually mark an auction as canceled (or another
# terminal status) after confirming against the source page directly.
#
# Writes a status_change event to auction_events (matching how every
# other status change in this pipeline is logged -- see load_csv.py's
# past-due sweep) BEFORE applying the UPDATE, in the same transaction,
# so a status is never changed without a matching audit-trail entry.
#
# Usage:
#   ./mark_canceled.sh <auction_id> [status] [db_path]
#
#   auction_id  required. The auctions.auction_id to update.
#   status      optional. Defaults to "canceled". Must be one of the
#               terminal statuses in statuses.py's EXPLICIT_TERMINAL_STATUSES.
#   db_path     optional. Defaults to auctionscout.db in the current directory.

set -euo pipefail

DEFAULT_DB="auctionscout.db"

# Mirrors statuses.py's EXPLICIT_TERMINAL_STATUSES -- kept in sync by hand
# (same situation as everywhere else this list gets duplicated outside
# Python; see DigestService.java's own cross-reference comments). If you
# add a new terminal status there, add it here too.
VALID_STATUSES=(
  "sold back to mortgagee"
  "3rd party purchase"
  "sold"
  "canceled"
  "cancelled"
  "withdrawn"
  "bank buy back"
)

print_usage() {
  echo "Usage: $0 <auction_id> [status] [db_path]"
  echo ""
  echo "  auction_id  required. The auctions.auction_id to update."
  echo "  status      optional. Defaults to \"canceled\". Must be one of:"
  printf '                %s\n' "${VALID_STATUSES[@]}"
  echo "  db_path     optional. Defaults to: ${DEFAULT_DB}"
}

if [[ $# -lt 1 ]]; then
  print_usage
  exit 1
fi

AUCTION_ID="$1"
STATUS="${2:-canceled}"
DB_PATH="${3:-$DEFAULT_DB}"

if [[ ! "$AUCTION_ID" =~ ^[0-9]+$ ]]; then
  echo "Error: auction_id must be numeric, got '${AUCTION_ID}'"
  echo ""
  print_usage
  exit 1
fi

valid=false
for s in "${VALID_STATUSES[@]}"; do
  if [[ "$s" == "$STATUS" ]]; then
    valid=true
    break
  fi
done
if [[ "$valid" != true ]]; then
  echo "Error: '${STATUS}' is not a recognized terminal status. Must be one of:"
  printf '  %s\n' "${VALID_STATUSES[@]}"
  exit 1
fi

if [[ ! -f "$DB_PATH" ]]; then
  echo "Error: database not found at ${DB_PATH}"
  exit 1
fi

# -separator lets us split the single-row output cleanly.
ROW=$(sqlite3 -separator '|' "$DB_PATH" \
  "SELECT p.address_raw, p.property_id, a.status
   FROM auctions a JOIN properties p ON p.property_id = a.property_id
   WHERE a.auction_id = ${AUCTION_ID};")

if [[ -z "$ROW" ]]; then
  echo "No auction found with auction_id = ${AUCTION_ID}"
  exit 1
fi

IFS='|' read -r ADDRESS PROPERTY_ID CURRENT_STATUS <<< "$ROW"

if [[ "$CURRENT_STATUS" == "$STATUS" ]]; then
  echo "Auction ${AUCTION_ID} is already status '${STATUS}'. Nothing to do."
  exit 0
fi

echo "Are you sure you want to remove: '${ADDRESS}' with property_id: ${PROPERTY_ID} and auction_id: ${AUCTION_ID}?"
echo "(status: ${CURRENT_STATUS} -> ${STATUS})"
read -r -p "[y/N] " CONFIRM
if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
  echo "Canceled -- no changes made."
  exit 0
fi

# -bail stops on the first error rather than pressing on to COMMIT after
# a failed statement (e.g. if spider_run_id turns out to be NOT NULL).
if ! sqlite3 -bail "$DB_PATH" <<SQL
BEGIN TRANSACTION;

INSERT INTO auction_events (auction_id, event_type, old_value, new_value, detected_at, spider_run_id)
SELECT auction_id, 'status_change', status, '${STATUS}', datetime('now'), NULL
FROM auctions WHERE auction_id = ${AUCTION_ID};

UPDATE auctions SET status = '${STATUS}', last_updated_at = datetime('now')
WHERE auction_id = ${AUCTION_ID};

COMMIT;
SQL
then
  echo "Error: update failed -- rolling back, no changes were made."
  # Harmless no-op if no transaction was actually left open.
  sqlite3 "$DB_PATH" "ROLLBACK;" 2>/dev/null || true
  exit 1
fi

echo "Done -- auction ${AUCTION_ID} marked '${STATUS}'."