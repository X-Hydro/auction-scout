"""
generate_email.py -- builds the HTML + plain-text body for AuctionScout's
auction digest emails (daily or weekly).

Two sections:
  1. UPCOMING -- auctions scheduled in the next N days (the core value:
     "what's happening this week"), grouped by day.
  2. STATUS CHANGES -- new listings, postponements (with new date), and
     cancellations since the last send.

CAVEAT ON "CANCELLED" -- read before trusting that section:
--------------------------------------------------------------------
get_cancelled_or_removed() mirrors export_json.py's EXACT live/excluded
logic (imports EXCLUDED_STATUSES and STALE_AFTER_DAYS directly from it),
so this email will never report something as "Cancelled" while it's
still showing live and clickable on the actual map -- an earlier version
of this file used load_csv.py's 'disappeared' event directly, which
fires immediately on a single missed scrape, up to STALE_AFTER_DAYS
(14) days before export_json.py actually hides the listing. That gap is
fixed: this now waits for the same staleness threshold the map itself
uses before reporting a removal.

One real ambiguity remains, and no timing fix can solve it: source data
doesn't always distinguish WHY a listing is gone. EXCLUDED_STATUSES
conflates genuinely-cancelled, sold, and withdrawn into one bucket --
if a spider only ever sees "sold back to mortgagee" text (never a plain
"cancelled"), this email will still say "Cancelled" for it, because
that's the most honest single word available given what the source
actually told us. If per-source status text ever gets more granular,
this can surface a real reason instead of a generic label.

Usage:
    python3 generate_email.py --db auctionscout.db --cadence weekly
    python3 generate_email.py --db auctionscout.db --cadence daily --states MA NH RI
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone
from html import escape

from export_json import EXCLUDED_STATUSES, STALE_AFTER_DAYS


# ---- data access --------------------------------------------------------

def _state_filter_clause(states, alias="p"):
    if not states:
        return "", []
    placeholders = ",".join("?" for _ in states)
    return f"AND {alias}.state IN ({placeholders})", list(states)


def get_upcoming_auctions(conn, start_dt: datetime, end_dt: datetime, states=None):
    """Auctions scheduled between start_dt and end_dt (inclusive), sorted
    chronologically. Excludes anything with no parseable auction_datetime
    (nothing to show a date for) -- those are a data-quality issue to fix
    at the scraper level, not something to silently guess at here."""
    clause, params = _state_filter_clause(states)
    rows = conn.execute(
        f"""
        SELECT p.property_id, p.address_raw, p.state, p.county, p.municipality,
               p.latitude, p.longitude,
               a.auction_datetime, a.auction_datetime_raw, a.status,
               a.property_type, a.bedrooms, a.bathrooms, a.sqft,
               a.source_url
        FROM auctions a
        JOIN properties p ON p.property_id = a.property_id
        WHERE a.auction_datetime IS NOT NULL
          AND a.auction_datetime BETWEEN ? AND ?
          {clause}
        ORDER BY a.auction_datetime ASC
        """,
        (start_dt.isoformat(), end_dt.isoformat(), *params),
    ).fetchall()
    cols = ["property_id", "address", "state", "county", "municipality", "latitude", "longitude",
            "auction_datetime", "auction_datetime_raw", "status", "property_type",
            "bedrooms", "bathrooms", "sqft", "source_url"]
    return [dict(zip(cols, r)) for r in rows]


def get_new_listings(conn, since_iso: str, states=None):
    clause, params = _state_filter_clause(states)
    rows = conn.execute(
        f"""
        SELECT p.address_raw, a.auction_datetime, a.auction_datetime_raw,
               a.source_url, e.detected_at
        FROM auction_events e
        JOIN auctions a ON a.auction_id = e.auction_id
        JOIN properties p ON p.property_id = a.property_id
        WHERE e.event_type = 'first_seen' AND e.detected_at >= ?
          {clause}
        ORDER BY e.detected_at DESC
        """,
        (since_iso, *params),
    ).fetchall()
    cols = ["address", "auction_datetime", "auction_datetime_raw", "source_url", "detected_at"]
    return [dict(zip(cols, r)) for r in rows]


def get_postponed(conn, since_iso: str, states=None):
    """Status changed to 'postponed'. Looks up a co-occurring date_change
    event from the SAME spider_run_id to report the new date -- if none
    is found (postponed with no new date announced yet), new_date is None
    and the email should say so rather than show a blank."""
    clause, params = _state_filter_clause(states, alias="p")
    status_events = conn.execute(
        f"""
        SELECT e.auction_id, e.old_value, e.new_value, e.detected_at,
               e.spider_run_id, p.address_raw, a.auction_datetime_raw,
               a.source_url
        FROM auction_events e
        JOIN auctions a ON a.auction_id = e.auction_id
        JOIN properties p ON p.property_id = a.property_id
        WHERE e.event_type = 'status_change'
          AND lower(e.new_value) = 'postponed'
          AND e.detected_at >= ?
          {clause}
        ORDER BY e.detected_at DESC
        """,
        (since_iso, *params),
    ).fetchall()

    results = []
    for auction_id, old_status, new_status, detected_at, run_id, address, orig_dt_raw, url in status_events:
        date_row = conn.execute(
            """SELECT new_value FROM auction_events
               WHERE auction_id = ? AND spider_run_id = ? AND event_type = 'date_change'""",
            (auction_id, run_id),
        ).fetchone()
        new_date_iso = date_row[0] if date_row else None
        results.append({
            "address": address,
            "original_datetime_raw": orig_dt_raw,
            "new_date_iso": new_date_iso,
            "source_url": url,
            "detected_at": detected_at,
        })
    return results


def get_cancelled_or_removed(conn, since_dt: datetime, until_dt: datetime, states=None):
    """
    Mirrors export_json.py's EXACT live/excluded logic, so the email never
    reports something as "Cancelled" while it's still showing live and
    clickable on the actual map -- or the reverse. Two distinct signals:

    1. EXPLICIT terminal status: a.status landed on one of
       export_json.py's own EXCLUDED_STATUSES (sold, withdrawn, etc.) via
       a status_change event within the window. This is a confirmed
       signal (the source itself said so) -- reported immediately.

    2. STALE exclusion: a property's last_seen_at crosses
       export_json.py's STALE_AFTER_DAYS threshold DURING this window.
       Reported at the exact moment export_json.py would start hiding it
       from the live map -- not the moment load_csv.py's 'disappeared'
       event first fires, which can be up to STALE_AFTER_DAYS earlier.
       This is the fix for the real gap: a listing that briefly vanished
       (a scraper miss that self-corrects) never gets falsely reported
       as "Cancelled" here, because it never actually goes stale.

    Still true, and worth remembering: EXCLUDED_STATUSES conflates
    genuinely-cancelled, sold, and withdrawn into one bucket -- the
    underlying source data doesn't reliably distinguish these. This fix
    solves the TIMING problem (email matching what the map shows), not
    the labeling ambiguity in the source data itself.
    """
    clause, params = _state_filter_clause(states, alias="p")

    # --- signal 1: explicit terminal status change within the window ---
    status_placeholders = ",".join("?" for _ in EXCLUDED_STATUSES)
    explicit_rows = conn.execute(
        f"""
        SELECT p.address_raw, a.auction_datetime_raw, a.source_url,
               e.detected_at, e.new_value, e.auction_id
        FROM auction_events e
        JOIN auctions a ON a.auction_id = e.auction_id
        JOIN properties p ON p.property_id = a.property_id
        WHERE e.event_type = 'status_change'
          AND lower(e.new_value) IN ({status_placeholders})
          AND e.detected_at >= ? AND e.detected_at < ?
          {clause}
        ORDER BY e.detected_at DESC
        """,
        (*[s.lower() for s in EXCLUDED_STATUSES], since_dt.isoformat(), until_dt.isoformat(), *params),
    ).fetchall()

    explicit_auction_ids = {r[5] for r in explicit_rows}
    results = [
        {"address": r[0], "auction_datetime_raw": r[1], "source_url": r[2],
         "detected_at": r[3], "reason": r[4]}
        for r in explicit_rows
    ]

    # --- signal 2: crossed the staleness threshold during this window ---
    # A property crosses staleness at (last_seen_at + STALE_AFTER_DAYS).
    # We want that crossing point to fall inside [since_dt, until_dt),
    # which rearranges to: last_seen_at in
    # [since_dt - STALE_AFTER_DAYS, until_dt - STALE_AFTER_DAYS).
    stale_delta = timedelta(days=STALE_AFTER_DAYS)
    window_start = (since_dt - stale_delta).isoformat()
    window_end = (until_dt - stale_delta).isoformat()

    stale_rows = conn.execute(
        f"""
        SELECT p.address_raw, a.auction_datetime_raw, a.source_url,
               p.last_seen_at, a.status, a.auction_id
        FROM properties p
        JOIN auctions a ON a.property_id = p.property_id
        WHERE p.last_seen_at >= ? AND p.last_seen_at < ?
          AND lower(a.status) NOT IN ({status_placeholders})
          {clause}
        ORDER BY p.last_seen_at DESC
        """,
        (window_start, window_end, *[s.lower() for s in EXCLUDED_STATUSES], *params),
    ).fetchall()

    for r in stale_rows:
        if r[5] in explicit_auction_ids:
            continue  # already reported via the explicit-status signal above
        results.append({
            "address": r[0], "auction_datetime_raw": r[1], "source_url": r[2],
            "detected_at": r[3], "reason": "no longer confirmed by source",
        })

    return results


# ---- formatting helpers --------------------------------------------------

def _fmt_short_dt(iso_str):
    """'2026-07-13T10:00:00' -> '07/13 at 10:00 am'"""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return None
    hour12 = dt.hour % 12 or 12
    ampm = "am" if dt.hour < 12 else "pm"
    return f"{dt.month:02d}/{dt.day:02d} at {hour12}:{dt.minute:02d} {ampm}"


def _fmt_date_only(iso_str):
    """'2026-08-13T10:00:00' -> '08/13'"""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return None
    return f"{dt.month:02d}/{dt.day:02d}"


def _fmt_day_header(iso_str):
    """'2026-07-13T10:00:00' -> 'Monday, July 13'"""
    dt = datetime.fromisoformat(iso_str)
    return dt.strftime("%A, %B ") + str(dt.day)


def _group_by_day(upcoming):
    groups = {}
    for row in upcoming:
        day_key = row["auction_datetime"][:10]
        groups.setdefault(day_key, []).append(row)
    return groups


# ---- rendering -------------------------------------------------------

ONCOORD_MAP_BASES = {
    "prod": "https://www.oncoord.com/auction-scout",
    "local": "http://localhost:8080/auction-scout",
}
DEFAULT_MAP_ZOOM = 16


def _oncoord_map_url(lat, lon, map_base):
    """Deep-links into the real AuctionScout map, centered on this
    property's coordinates -- see the lat/lng/zoom handling added to
    auction-scout.js's init(). Deliberately simple: no property lookup,
    no stale-listing edge case -- the user clicks the marker themselves
    once the map is centered there.

    map_base is one of ONCOORD_MAP_BASES's values, e.g. the deployed
    oncoord.com URL for real sends, or localhost for testing against a
    dev server -- see the --env flag."""
    if lat is None or lon is None:
        return None
    return f"{map_base}?lat={lat}&lng={lon}&zoom={DEFAULT_MAP_ZOOM}"


def _spec_line(row):
    parts = []
    if row.get("property_type"):
        parts.append(row["property_type"])
    if row.get("bedrooms"):
        parts.append(f"{row['bedrooms']} bed")
    if row.get("bathrooms"):
        parts.append(f"{row['bathrooms']} bath")
    if row.get("sqft"):
        parts.append(f"{row['sqft']:,} sf")
    return " · ".join(parts)


def render_html(upcoming, new_listings, postponed, cancelled, period_label, map_base):
    css = """
        body { font-family: -apple-system, Helvetica, Arial, sans-serif; color: #1a1a1a; margin:0; padding:0; background:#f4f4f4; }
        .container { max-width: 640px; margin: 0 auto; background:#ffffff; }
        .header { background:#1a3a5c; color:#ffffff; padding:24px 32px; }
        .header h1 { margin:0; font-size:20px; }
        .header p { margin:4px 0 0; font-size:13px; opacity:0.85; }
        .section { padding:24px 32px; border-bottom:1px solid #eaeaea; }
        .section h2 { font-size:16px; margin:0 0 16px; color:#1a3a5c; }
        .day-header { font-size:13px; font-weight:600; color:#666; margin:16px 0 8px; text-transform:uppercase; letter-spacing:0.03em; }
        .listing { padding:10px 0; border-bottom:1px solid #f0f0f0; }
        .listing:last-child { border-bottom:none; }
        .listing .addr { font-weight:600; font-size:14px; }
        .listing .meta { font-size:13px; color:#666; margin-top:2px; }
        .listing a { color:#1a5c9c; text-decoration:none; font-size:13px; }
        table.status-table { width:100%; border-collapse:collapse; font-size:13px; }
        table.status-table td { padding:8px 4px; border-bottom:1px solid #f0f0f0; }
        .tag { display:inline-block; padding:2px 8px; border-radius:3px; font-size:11px; font-weight:600; }
        .tag-new { background:#e6f4ea; color:#1e7e34; }
        .tag-postponed { background:#fff4e5; color:#b25e00; }
        .tag-cancelled { background:#fdecea; color:#b3261e; }
        .empty { color:#999; font-size:13px; font-style:italic; }
        .footer { padding:20px 32px; font-size:11px; color:#999; }
    """
    parts = [f"<html><head><style>{css}</style></head><body><div class='container'>"]
    parts.append(f"<div class='header'><h1>AuctionScout Auction Watch</h1><p>{escape(period_label)}</p></div>")

    # --- upcoming ---
    parts.append("<div class='section'><h2>Auctions in the Next 7 Days</h2>")
    if not upcoming:
        parts.append("<p class='empty'>No auctions scheduled in this window.</p>")
    else:
        by_day = _group_by_day(upcoming)
        for day_key in sorted(by_day):
            parts.append(f"<div class='day-header'>{escape(_fmt_day_header(by_day[day_key][0]['auction_datetime']))}</div>")
            for row in by_day[day_key]:
                loc = ", ".join(x for x in [row.get("municipality") or row.get("county"), row.get("state")] if x)
                spec = _spec_line(row)
                map_url = _oncoord_map_url(row.get("latitude"), row.get("longitude"), map_base)
                links = f"<a href='{escape(row['source_url'])}'>View listing →</a>"
                if map_url:
                    links += f" &nbsp;·&nbsp; <a href='{escape(map_url)}'>View map →</a>"
                parts.append(
                    "<div class='listing'>"
                    f"<div class='addr'>{escape(row['address'])}</div>"
                    f"<div class='meta'>{escape(_fmt_short_dt(row['auction_datetime']) or row['auction_datetime_raw'])}"
                    f"{' · ' + escape(spec) if spec else ''}</div>"
                    f"{links}"
                    "</div>"
                )
    parts.append("</div>")

    # --- status changes ---
    parts.append("<div class='section'><h2>Status Updates</h2>")
    if not (new_listings or postponed or cancelled):
        parts.append("<p class='empty'>No status changes since the last update.</p>")
    else:
        parts.append("<table class='status-table'>")
        for row in new_listings:
            dt = _fmt_short_dt(row["auction_datetime"]) or row["auction_datetime_raw"]
            parts.append(
                f"<tr><td>{escape(row['address'])}</td><td>{escape(dt)}</td>"
                f"<td><span class='tag tag-new'>New</span></td></tr>"
            )
        for row in postponed:
            new_date = _fmt_date_only(row["new_date_iso"])
            change_text = f"Postponed → {new_date}" if new_date else "Postponed (new date TBD)"
            parts.append(
                f"<tr><td>{escape(row['address'])}</td><td>{escape(row['original_datetime_raw'])}</td>"
                f"<td><span class='tag tag-postponed'>{escape(change_text)}</span></td></tr>"
            )
        for row in cancelled:
            parts.append(
                f"<tr><td>{escape(row['address'])}</td><td>{escape(row['auction_datetime_raw'] or '')}</td>"
                f"<td><span class='tag tag-cancelled'>Cancelled</span></td></tr>"
            )
        parts.append("</table>")
    parts.append("</div>")

    parts.append(
        "<div class='footer'>You're receiving this because you subscribed to AuctionScout Auction Watch. "
        "<a href='#'>Manage preferences</a> · <a href='#'>Unsubscribe</a></div>"
    )
    parts.append("</div></body></html>")
    return "\n".join(parts)


def render_text(upcoming, new_listings, postponed, cancelled, period_label, map_base):
    L = []
    L.append("AUCTIONSCOUT AUCTION WATCH")
    L.append(period_label)
    L.append("=" * 60)
    L.append("")
    L.append("AUCTIONS IN THE NEXT 7 DAYS")
    L.append("-" * 60)
    if not upcoming:
        L.append("  (none scheduled)")
    else:
        by_day = _group_by_day(upcoming)
        for day_key in sorted(by_day):
            L.append("")
            L.append(_fmt_day_header(by_day[day_key][0]["auction_datetime"]).upper())
            for row in by_day[day_key]:
                dt = _fmt_short_dt(row["auction_datetime"]) or row["auction_datetime_raw"]
                spec = _spec_line(row)
                line = f"  {row['address']:<45} {dt}"
                if spec:
                    line += f"  ({spec})"
                L.append(line)
                L.append(f"    Listing: {row['source_url']}")
                map_url = _oncoord_map_url(row.get("latitude"), row.get("longitude"), map_base)
                if map_url:
                    L.append(f"    Map:     {map_url}")
    L.append("")
    L.append("STATUS UPDATES")
    L.append("-" * 60)
    if not (new_listings or postponed or cancelled):
        L.append("  (no changes since the last update)")
    else:
        for row in new_listings:
            dt = _fmt_short_dt(row["auction_datetime"]) or row["auction_datetime_raw"]
            L.append(f"  {row['address']:<40}\t{dt}\tNew")
        for row in postponed:
            new_date = _fmt_date_only(row["new_date_iso"])
            change_text = f"Postponed → {new_date}" if new_date else "Postponed (new date TBD)"
            L.append(f"  {row['address']:<40}\t{row['original_datetime_raw']}\t{change_text}")
        for row in cancelled:
            L.append(f"  {row['address']:<40}\t{row['auction_datetime_raw'] or '':<20}\tCancelled")
    L.append("")
    L.append("=" * 60)
    L.append("Manage preferences / unsubscribe: [link]")
    return "\n".join(L)


# ---- orchestration ------------------------------------------------------

def build_email(db_path: str, cadence: str = "weekly", states=None, now: datetime = None,
                env: str = "prod") -> dict:
    now = now or datetime.now(timezone.utc)
    upcoming_end = now + timedelta(days=7)
    since = now - timedelta(days=1 if cadence == "daily" else 7)

    try:
        map_base = ONCOORD_MAP_BASES[env]
    except KeyError:
        raise ValueError(f"Unknown env {env!r}; expected one of {sorted(ONCOORD_MAP_BASES)}")

    conn = sqlite3.connect(db_path)
    try:
        upcoming = get_upcoming_auctions(conn, now, upcoming_end, states=states)
        new_listings = get_new_listings(conn, since.isoformat(), states=states)
        postponed = get_postponed(conn, since.isoformat(), states=states)
        cancelled = get_cancelled_or_removed(conn, since, now, states=states)
    finally:
        conn.close()

    period_label = f"{'Daily' if cadence == 'daily' else 'Weekly'} update — {now.strftime('%B %d, %Y')}"
    subject = f"AuctionScout Auction Watch: {len(upcoming)} auction(s) this week"
    if cancelled:
        subject += f", {len(cancelled)} cancelled"

    return {
        "subject": subject,
        "html": render_html(upcoming, new_listings, postponed, cancelled, period_label, map_base),
        "text": render_text(upcoming, new_listings, postponed, cancelled, period_label, map_base),
        "counts": {
            "upcoming": len(upcoming),
            "new": len(new_listings),
            "postponed": len(postponed),
            "cancelled_or_removed": len(cancelled),
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate the AuctionScout auction digest email body.")
    parser.add_argument("--db", required=True, help="Path to auctionscout.db")
    parser.add_argument("--cadence", choices=["daily", "weekly"], default="weekly")
    parser.add_argument("--states", nargs="+", default=None, help="Filter to specific states, e.g. --states MA NH RI")
    parser.add_argument("--env", choices=sorted(ONCOORD_MAP_BASES), default="prod",
                        help="Which AuctionScout map the 'View map' links point at: "
                             "'prod' (https://www.oncoord.com/auction-scout, default) or "
                             "'local' (http://localhost:8080/auction-scout, for testing against a dev server)")
    parser.add_argument("--out-html", default="email_preview.html")
    parser.add_argument("--out-text", default="email_preview.txt")
    args = parser.parse_args()

    result = build_email(args.db, cadence=args.cadence, states=args.states, env=args.env)
    print(f"Subject: {result['subject']}")
    print(f"Counts: {result['counts']}")

    with open(args.out_html, "w", encoding="utf-8") as f:
        f.write(result["html"])
    with open(args.out_text, "w", encoding="utf-8") as f:
        f.write(result["text"])
    print(f"Wrote {args.out_html} and {args.out_text}")