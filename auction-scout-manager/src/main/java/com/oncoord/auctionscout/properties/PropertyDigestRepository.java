package com.oncoord.auctionscout.properties;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.time.LocalDateTime;
import java.time.OffsetDateTime;
import java.util.List;

/**
 * Reads from auctionscout.db (properties/auctions/auction_events),
 * written by the Python scraping pipeline. This service never writes
 * to it.
 *
 * Deliberately "dumb": no status interpretation, no filtering by
 * status text, no mapping unknown values to a fixed set of tags.
 * Real-world status values (confirmed by querying live data) don't
 * match the schema comment's documented "on|postponed|canceled|sold"
 * vocabulary at all — actual values include "active", "on_time",
 * "sold back to mortgagee", "3rd party purchase", and at least one
 * value ("selling absolute above $100k") that doesn't look like a
 * lifecycle status at all. Rather than guess a mapping and risk
 * silently hiding rows the way an earlier version of this file did,
 * this layer just passes whatever's in the database straight through.
 * Data cleanup is happening upstream in the Python pipeline instead.
 *
 * Two DIFFERENT date formats, confirmed against real rows — NOT a bug,
 * two different parsers used deliberately:
 *   - auction_datetime: local time, no offset, e.g. "2026-07-14T10:00:00"
 *     (an auction's 10am is 10am in New England regardless of server tz)
 *   - detected_at: UTC with offset and microseconds, e.g.
 *     "2026-07-09T17:52:56.038411+00:00" (a pipeline bookkeeping timestamp)
 */
@Repository
public class PropertyDigestRepository {

    public record UpcomingListing(
            long propertyId, String address, String state, Double latitude, Double longitude,
            String sourceUrl, LocalDateTime auctionDateTime,
            OffsetDateTime firstSeenAt, OffsetDateTime lastSeenAt) {}

    public record ChangedListing(
            long propertyId, String address, String state, String eventType,
            String oldValue, String newValue, LocalDateTime auctionDateTime,
            OffsetDateTime firstSeenAt, OffsetDateTime lastSeenAt,
            OffsetDateTime detectedAt,
            String sourceUrl, Double latitude, Double longitude) {}

    private final PropertiesDbConnectionManager dbManager;

    public PropertyDigestRepository(PropertiesDbConnectionManager dbManager) {
        this.dbManager = dbManager;
    }

    /**
     * All auctions in the given date window, for the given states — no
     * STATUS-TEXT filtering at all. If a sold/cancelled listing has a
     * stale future auction_datetime, it'll show up here; that's a data
     * problem to fix at the source, not something this query papers
     * over by guessing which status strings count as "still upcoming".
     *
     * One exception, and it's NOT a status-text guess: auctions whose
     * most recent auction_events row is 'disappeared' are excluded.
     * That event means the pipeline structurally observed this listing
     * vanish from its source on a later scrape — a fact from the audit
     * trail, not an interpretation of a status string — so it has no
     * business appearing as "upcoming" regardless of what stale
     * auction_datetime or status text is still sitting on the row.
     *
     * NOT currently called by DigestService — both the emailed digest
     * and the status page source their "upcoming" list from findActive()
     * + DigestService.filterActiveListings() instead, so the two surfaces
     * show identical properties. Left in place as a plain bounded-window
     * query in case something else needs one; delete if it stays unused.
     */
    public List<UpcomingListing> findUpcoming(List<String> states, LocalDateTime from, LocalDateTime to) {
        if (states.isEmpty()) {
            return List.of();
        }
        String placeholders = String.join(",", states.stream().map(s -> "?").toList());

        String sql = """
                SELECT p.property_id, p.address_raw, p.state, p.latitude, p.longitude,
                       a.source_url, a.auction_datetime, p.first_seen_at, p.last_seen_at
                FROM auctions a
                JOIN properties p ON p.property_id = a.property_id
                WHERE a.auction_datetime BETWEEN ? AND ?
                  AND p.state IN (%s)
                  AND NOT EXISTS (
                      SELECT 1 FROM property_duplicate_links d WHERE d.property_id = p.property_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM auction_events e
                      WHERE e.auction_id = a.auction_id
                        AND e.event_type = 'disappeared'
                        AND e.event_id = (
                            SELECT MAX(event_id) FROM auction_events WHERE auction_id = a.auction_id
                        )
                  )
                ORDER BY a.auction_datetime
                """.formatted(placeholders);

        Object[] args = buildArgs(from.toString(), to.toString(), states);

        JdbcTemplate jdbc = dbManager.getJdbcTemplate();
        return jdbc.query(sql, (rs, rowNum) -> new UpcomingListing(
                rs.getLong("property_id"),
                rs.getString("address_raw"),
                rs.getString("state"),
                (Double) rs.getObject("latitude"),
                (Double) rs.getObject("longitude"),
                rs.getString("source_url"),
                parseLocal(rs.getString("auction_datetime")),
                parseOffset(rs.getString("first_seen_at")),
                parseOffset(rs.getString("last_seen_at"))
        ), args);
    }

    /**
     * Same shape as findUpcoming, but open-ended on the future side (no
     * "to" bound) -- backs the seasoned "active auctions" view, which
     * isn't a near-term reminder window like findUpcoming's callers use
     * it for. DigestService applies the 30-day cap and 7-day
     * seasoning/urgency rule on top of this at the application layer,
     * not here, so both callers can share one query.
     */
    public List<UpcomingListing> findActive(List<String> states, LocalDateTime from) {
        if (states.isEmpty()) {
            return List.of();
        }
        String placeholders = String.join(",", states.stream().map(s -> "?").toList());

        String sql = """
                SELECT p.property_id, p.address_raw, p.state, p.latitude, p.longitude,
                       a.source_url, a.auction_datetime, p.first_seen_at, p.last_seen_at
                FROM auctions a
                JOIN properties p ON p.property_id = a.property_id
                WHERE a.auction_datetime >= ?
                  AND p.state IN (%s)
                  AND NOT EXISTS (
                      SELECT 1 FROM property_duplicate_links d WHERE d.property_id = p.property_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM auction_events e
                      WHERE e.auction_id = a.auction_id
                        AND e.event_type = 'disappeared'
                        AND e.event_id = (
                            SELECT MAX(event_id) FROM auction_events WHERE auction_id = a.auction_id
                        )
                  )
                ORDER BY a.auction_datetime
                """.formatted(placeholders);

        Object[] args = buildArgs(from.toString(), states);

        JdbcTemplate jdbc = dbManager.getJdbcTemplate();
        return jdbc.query(sql, (rs, rowNum) -> new UpcomingListing(
                rs.getLong("property_id"),
                rs.getString("address_raw"),
                rs.getString("state"),
                (Double) rs.getObject("latitude"),
                (Double) rs.getObject("longitude"),
                rs.getString("source_url"),
                parseLocal(rs.getString("auction_datetime")),
                parseOffset(rs.getString("first_seen_at")),
                parseOffset(rs.getString("last_seen_at"))
        ), args);
    }

    /**
     * Every first_seen/status_change/date_change event since the given
     * cutoff, for the given states — raw event_type/old_value/new_value
     * passed straight through, no interpretation. One row per event,
     * not deduplicated per auction, so a single auction with multiple
     * events in the window produces multiple rows.
     *
     * One narrow exception to the "no status filtering" rule: events for
     * a listing that's both dateless (auction_datetime IS NULL) AND
     * already in a terminal state (cancelled/sold/3rd-party-purchase)
     * are excluded. Confirmed via live data that every current
     * NULL-date row falls into exactly this set — these are typically
     * Harmon listings discovered via ID-range probing that were already
     * closed by the time they were first scraped, so a "New" alert for
     * one is pure noise, not a real change a subscriber cares about.
     * This isn't reintroducing a status taxonomy; it's excluding rows
     * that carry zero actionable information regardless of what the
     * status string says.
     * "disappeared" events ARE included here (unlike load_csv.py's raw
     * DB terminology) but are relabeled "Removed" at render time in
     * DigestService -- the internal event_type name stays "disappeared"
     * in the database; every subscriber-facing surface says "Removed"
     * instead, since we don't actually know what happened (sold,
     * cancelled, or a scraper miss are all indistinguishable from this
     * event alone) and "disappeared" reads oddly as a listing status.
     */
    public List<ChangedListing> findRecentChanges(List<String> states, OffsetDateTime since) {
        if (states.isEmpty()) {
            return List.of();
        }
        String placeholders = String.join(",", states.stream().map(s -> "?").toList());

        String sql = """
                SELECT p.property_id, p.address_raw, p.state, p.first_seen_at, p.last_seen_at, a.auction_datetime,
                       e.event_type, e.old_value, e.new_value, e.detected_at,
                       a.source_url, p.latitude, p.longitude
                FROM auction_events e
                JOIN auctions a ON a.auction_id = e.auction_id
                JOIN properties p ON p.property_id = a.property_id
                WHERE e.detected_at >= ?
                  AND p.state IN (%s)
                  AND NOT EXISTS (
                      SELECT 1 FROM property_duplicate_links d WHERE d.property_id = p.property_id
                  )
                  AND NOT (
                      a.auction_datetime IS NULL
                      AND a.status IN ('cancelled', 'sold back to mortgagee', '3rd party purchase')
                  )
                ORDER BY e.detected_at DESC
                """.formatted(placeholders);

        Object[] args = buildArgs(since.toString(), states);

        JdbcTemplate jdbc = dbManager.getJdbcTemplate();
        return jdbc.query(sql, (rs, rowNum) -> new ChangedListing(
                rs.getLong("property_id"),
                rs.getString("address_raw"),
                rs.getString("state"),
                rs.getString("event_type"),
                rs.getString("old_value"),
                rs.getString("new_value"),
                parseLocal(rs.getString("auction_datetime")),
                parseOffset(rs.getString("first_seen_at")),
                parseOffset(rs.getString("last_seen_at")),
                parseOffset(rs.getString("detected_at")),
                rs.getString("source_url"),
                (Double) rs.getObject("latitude"),
                (Double) rs.getObject("longitude")
        ), args);
    }

    private static Object[] buildArgs(String a, String b, List<String> states) {
        Object[] args = new Object[2 + states.size()];
        args[0] = a;
        args[1] = b;
        for (int i = 0; i < states.size(); i++) {
            args[2 + i] = states.get(i);
        }
        return args;
    }

    private static Object[] buildArgs(String a, List<String> states) {
        Object[] args = new Object[1 + states.size()];
        args[0] = a;
        for (int i = 0; i < states.size(); i++) {
            args[1 + i] = states.get(i);
        }
        return args;
    }

    // auction_datetime: local, no offset
    private static LocalDateTime parseLocal(String raw) {
        if (raw == null) return null;
        try {
            return LocalDateTime.parse(raw);
        } catch (Exception e) {
            return null;
        }
    }

    // first_seen_at / detected_at: UTC with offset and microseconds
    private static OffsetDateTime parseOffset(String raw) {
        if (raw == null) return null;
        try {
            return OffsetDateTime.parse(raw);
        } catch (Exception e) {
            return null;
        }
    }
}