package com.oncoord.auctionscout.testsupport;

import org.springframework.jdbc.core.JdbcTemplate;

/**
 * Inserts real rows into properties/auctions/auction_events for tests
 * that exercise queries against those tables (PropertyDigestRepository,
 * DigestService, and anything else built on the same schema going
 * forward) -- deliberately NOT a mock. A mock only proves a method was
 * called with hand-built objects; these builders let the real SQL in
 * the repository run against real rows, which is the only way to
 * catch drift between a query and the schema it assumes.
 *
 * Each builder ships with defaults that produce a single valid,
 * "boring" row (one property, one auction 6 days out, no events) so a
 * test only has to override the 1-2 fields it's actually asserting
 * on. (source, source_listing_id) is auto-generated per property()
 * call to satisfy the UNIQUE constraint without every call site
 * having to invent one.
 *
 * Usage:
 * <pre>
 *   PropertyDigestTestData fixtures = new PropertyDigestTestData(jdbc);
 *   long propertyId = fixtures.property().address("42 Elm St, Nashua, NH").state("NH").insert();
 *   long auctionId = fixtures.auction(propertyId).auctionDatetime("2026-07-20T10:00:00").insert();
 *   fixtures.event(auctionId, "date_change")
 *       .oldValue("2026-07-15T10:00:00")
 *       .newValue("2026-07-20T10:00:00")
 *       .insert();
 * </pre>
 */
public final class PropertyDigestTestData {

    private final JdbcTemplate jdbc;
    private int propertyCounter = 0;

    public PropertyDigestTestData(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public PropertyBuilder property() {
        propertyCounter++;
        return new PropertyBuilder("fixture-source", "fixture-" + propertyCounter);
    }

    public AuctionBuilder auction(long propertyId) {
        return new AuctionBuilder(propertyId);
    }

    public EventBuilder event(long auctionId, String eventType) {
        return new EventBuilder(auctionId, eventType);
    }

    // ---- properties -------------------------------------------------

    public final class PropertyBuilder {
        private final String source;
        private final String sourceListingId;
        private String addressRaw = "123 Test Street, Nashua, NH";
        private Double latitude = 42.7654;
        private Double longitude = -71.4676;
        private String state = "NH";
        // Well before any changesSince cutoff a test is likely to use,
        // and far enough before lastSeenAt to clear the digest's
        // MIN_HISTORY_DAYS "New" suppression window by default.
        private String firstSeenAt = "2026-06-01T08:00:00.000000+00:00";
        private String lastSeenAt = "2026-07-14T08:00:00.000000+00:00";

        private PropertyBuilder(String source, String sourceListingId) {
            this.source = source;
            this.sourceListingId = sourceListingId;
        }

        public PropertyBuilder address(String addressRaw) {
            this.addressRaw = addressRaw;
            return this;
        }

        public PropertyBuilder state(String state) {
            this.state = state;
            return this;
        }

        public PropertyBuilder latLng(double latitude, double longitude) {
            this.latitude = latitude;
            this.longitude = longitude;
            return this;
        }

        public PropertyBuilder noLocation() {
            this.latitude = null;
            this.longitude = null;
            return this;
        }

        public PropertyBuilder firstSeenAt(String firstSeenAt) {
            this.firstSeenAt = firstSeenAt;
            return this;
        }

        public PropertyBuilder lastSeenAt(String lastSeenAt) {
            this.lastSeenAt = lastSeenAt;
            return this;
        }

        public long insert() {
            jdbc.update("""
                    INSERT INTO properties
                        (source, source_listing_id, address_raw, latitude, longitude, state, first_seen_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    source, sourceListingId, addressRaw, latitude, longitude, state, firstSeenAt, lastSeenAt);
            return jdbc.queryForObject(
                    "SELECT property_id FROM properties WHERE source = ? AND source_listing_id = ?",
                    Long.class, source, sourceListingId);
        }
    }

    // ---- auctions -----------------------------------------------------

    public final class AuctionBuilder {
        private final long propertyId;
        // 6 days out by default -- inside DigestService's default
        // 7-day upcoming window without a test having to think about it.
        private String auctionDatetime = "2026-07-20T10:00:00";
        private String status = "active";
        private String sourceUrl = "https://example.com/listing/test";
        private String lastUpdatedAt = "2026-07-14T08:00:00.000000+00:00";

        private AuctionBuilder(long propertyId) {
            this.propertyId = propertyId;
        }

        public AuctionBuilder auctionDatetime(String auctionDatetime) {
            this.auctionDatetime = auctionDatetime;
            return this;
        }

        public AuctionBuilder noAuctionDatetime() {
            this.auctionDatetime = null;
            return this;
        }

        public AuctionBuilder status(String status) {
            this.status = status;
            return this;
        }

        public AuctionBuilder sourceUrl(String sourceUrl) {
            this.sourceUrl = sourceUrl;
            return this;
        }

        public long insert() {
            jdbc.update("""
                    INSERT INTO auctions
                        (property_id, auction_datetime, status, source_url, last_updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    propertyId, auctionDatetime, status, sourceUrl, lastUpdatedAt);
            // Safe to look up by property_id alone: auctions has
            // UNIQUE(property_id), so there's at most one row per property.
            return jdbc.queryForObject(
                    "SELECT auction_id FROM auctions WHERE property_id = ?", Long.class, propertyId);
        }
    }

    // ---- auction_events -------------------------------------------------

    public final class EventBuilder {
        private final long auctionId;
        private final String eventType;
        private String oldValue;
        private String newValue;
        // After the 2026-06-01 default firstSeenAt and before "now" in
        // tests fixed around mid-July 2026 -- adjust per test if your
        // changesSince cutoff needs it earlier or later.
        private String detectedAt = "2026-07-14T09:00:00.038411+00:00";

        private EventBuilder(long auctionId, String eventType) {
            this.auctionId = auctionId;
            this.eventType = eventType;
        }

        public EventBuilder oldValue(String oldValue) {
            this.oldValue = oldValue;
            return this;
        }

        public EventBuilder newValue(String newValue) {
            this.newValue = newValue;
            return this;
        }

        public EventBuilder detectedAt(String detectedAt) {
            this.detectedAt = detectedAt;
            return this;
        }

        public void insert() {
            jdbc.update("""
                    INSERT INTO auction_events (auction_id, event_type, old_value, new_value, detected_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    auctionId, eventType, oldValue, newValue, detectedAt);
        }
    }
}