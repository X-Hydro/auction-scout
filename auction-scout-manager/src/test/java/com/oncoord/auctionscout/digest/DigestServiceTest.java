package com.oncoord.auctionscout.digest;

import com.oncoord.auctionscout.properties.PropertiesDbConnectionManager;
import com.oncoord.auctionscout.properties.PropertyDigestRepository;
import com.oncoord.auctionscout.testsupport.PropertyDigestTestData;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInfo;
import org.springframework.core.io.FileSystemResource;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.SingleConnectionDataSource;
import org.springframework.jdbc.datasource.init.ScriptUtils;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.SQLException;
import java.time.LocalDateTime;
import java.time.OffsetDateTime;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Exercises DigestService against a real SQLite file on disk, same
 * philosophy as AuctionScoutTokenStoreTest: PropertyDigestRepository
 * is NOT mocked. Mocking it would only prove renderChanges() was
 * called with whatever ChangedListing objects we hand-built -- it
 * would say nothing about whether the actual SQL in
 * findRecentChanges() (the JOINs, the state filter, the
 * property_duplicate_links exclusion, the two different date parsers)
 * lines up with the schema. Inserting real rows and letting the real
 * query run is the only way to catch drift between the two.
 *
 * Uses ../schema.sql -- the properties/auctions/auction_events schema
 * for auctionscout.db, written by the Python scraping pipeline. This
 * is a DIFFERENT database and schema from auction-scout-manager.sql
 * (the auth/login db AuctionScoutTokenStoreTest uses); the two are
 * unrelated on disk and just happen to sit in sibling directories.
 *
 * DigestService.render() never touches SubscriberRepository (only
 * renderForSubscriber() does), so it's safe to pass null for that
 * dependency here rather than mock it.
 *
 * Each test's database file lands in src/test/db/, named after this
 * class, and is NOT deleted after the run -- intentionally, so it can
 * be inspected afterward with the sqlite3 CLI or DB Browser for
 * SQLite. @BeforeEach deletes and recreates it fresh each run.
 */
class DigestServiceTest {

    private static final Path TEST_DB_DIR = Path.of("src/test/db");
    private static final Path DB_PATH = TEST_DB_DIR.resolve("auction-scout-manager-digest.db");

    private SingleConnectionDataSource dataSource;
    private PropertyDigestTestData testData;
    private DigestService digestService;
    private PropertiesDbConnectionManager dbManager;


    @BeforeEach
    void setUp(TestInfo testInfo) throws IOException, SQLException {
        Files.createDirectories(TEST_DB_DIR);
        Files.deleteIfExists(DB_PATH);

        dataSource = new SingleConnectionDataSource("jdbc:sqlite:" + DB_PATH.toAbsolutePath(), true);
        try (Connection conn = dataSource.getConnection()) {
            // This is the properties/auctions/auction_events schema for
            // auctionscout.db (written by the Python scraping pipeline) --
            // a DIFFERENT database from the auth/login schema
            // (auction-scout-manager.sql) that AuctionScoutTokenStoreTest
            // uses. It lives one level up from this module's working
            // directory, as a sibling of auction-scout-manager/ (not
            // inside it), so it can't be loaded via ClassPathResource.
            Path schemaPath = Path.of("../auction-scout-data/schema.sql");
            if (!Files.exists(schemaPath)) {
                throw new IllegalStateException(
                        "Expected schema.sql at " + schemaPath.toAbsolutePath()
                                + " (relative to the test's working directory). "
                                + "If the repo layout or run-configuration working directory changed, "
                                + "update the schemaPath value above.");
            }
            ScriptUtils.executeSqlScript(conn, new FileSystemResource(schemaPath.toFile()));
        }

        JdbcTemplate jdbc = new JdbcTemplate(dataSource);
        dbManager = new PropertiesDbConnectionManager(DB_PATH.toString());
        testData = new PropertyDigestTestData(jdbc);
        PropertyDigestRepository repository = new PropertyDigestRepository(dbManager);
        // null for both SubscriberRepository and TokenService: render()
        // (the only method every test here calls) touches neither --
        // only renderForSubscriber() does, for the email auto-login
        // links. Same reasoning as the existing SubscriberRepository
        // null, just now applying to the newer TokenService param too.
        digestService = new DigestService(repository, null, null, "https://oncoord.com");
    }

    @AfterEach
    void closeConnection() {
        dbManager.close();
        dataSource.destroy();
        // DB file deliberately left on disk for inspection -- see class javadoc.
    }

    @Test
    void render_showsDateChangeTag_forListingWithOnlyADateChangeEvent() {
        long propertyId = testData.property()
                .address("42 Elm Street, Nashua, NH")
                .state("NH")
                .insert();
        long auctionId = testData.auction(propertyId)
                .auctionDatetime("2026-07-20T10:00:00")
                .insert();
        testData.event(auctionId, "date_change")
                .oldValue("2026-07-15T10:00:00")
                .newValue("2026-07-20T10:00:00")
                .insert(); // detected_at defaults to well after changesSince below

        String html = digestService.render(
                List.of("NH"),
                OffsetDateTime.parse("2026-07-01T00:00:00+00:00"),
                false
        );

        assertTrue(html.contains("42 Elm Street, Nashua, NH"),
                "expected the property address in the Status Changes section");
        assertTrue(html.contains("date_change: 2026-07-20T10:00:00"),
                "expected the raw event_type/new_value pass-through as the tag text");

        // A pure date_change (not paired with first_seen or disappeared
        // in this window) must NOT be relabeled New or Removed -- those
        // labels only apply when wasNew/wasRemoved actually fired.
        assertFalse(html.contains("class='tag'>New<"),
                "a lone date_change must not be shown as New");
        assertFalse(html.contains("class='tag'>Removed<"),
                "a lone date_change must not be shown as Removed");
    }

    @Test
    void render_excludesDateChange_whenStateDoesNotMatchSubscriberFilter() {
        long propertyId = testData.property()
                .address("10 Maple Ave, Manchester, NH")
                .state("NH")
                .insert();
        long auctionId = testData.auction(propertyId)
                .auctionDatetime("2026-07-20T10:00:00")
                .insert();
        testData.event(auctionId, "date_change")
                .oldValue("2026-07-15T10:00:00")
                .newValue("2026-07-20T10:00:00")
                .insert();

        // Subscriber only watches MA, not NH -- should see nothing.
        String html = digestService.render(
                List.of("MA"),
                OffsetDateTime.parse("2026-07-01T00:00:00+00:00"),
                false
        );

        assertFalse(html.contains("10 Maple Ave, Manchester, NH"));
        assertTrue(html.contains("No status changes to report this week."));
    }

    @Test
    void render_showsUpcomingAuction_inNext7DayWindow() {
        // auction_datetime is compared against LocalDateTime.now() inside
        // DigestService.render() itself, not a value this test controls --
        // so the date has to be relative to the real clock (now + 3 days)
        // rather than a fixed literal, or this test would start failing
        // the moment real time passed a hardcoded date.
        String auctionDateTime = LocalDateTime.now().withNano(0).plusDays(3).toString();

        long propertyId = testData.property()
                .address("5 Birch Lane, Concord, NH")
                .state("NH")
                .latLng(43.2081, -71.5376)
                .insert();
        testData.auction(propertyId)
                .auctionDatetime(auctionDateTime)
                .sourceUrl("https://example.com/listing/5001")
                .insert();

        String html = digestService.render(
                List.of("NH"),
                OffsetDateTime.now().minusYears(1), // no events inserted, so this just needs to not matter
                false
        );

        assertTrue(html.contains("5 Birch Lane, Concord, NH"),
                "expected the property address in the Upcoming Auctions section");
        assertTrue(html.contains("https://example.com/listing/5001"),
                "expected a View listing link to the source URL");
        assertTrue(html.contains("lat=43.2081&amp;lng=-71.5376") || html.contains("lat=43.2081&lng=-71.5376"),
                "expected a View map link built from the property's lat/lng");
        assertFalse(html.contains("No auctions in the next 7 days"),
                "an auction inside the window should suppress the empty-state message");
    }

    @Test
    void render_showsNewTag_forFirstSeenEvent() {
        long propertyId = testData.property()
                .address("8 Cedar Court, Manchester, NH")
                .state("NH")
                .insert();
        // auction_datetime left null: DigestService only runs its "New"
        // far-out/insufficient-history suppression check when
        // auctionDateTime is non-null, so this sidesteps that
        // clock-dependent logic entirely for a test that's just
        // asserting the plain positive case.
        long auctionId = testData.auction(propertyId)
                .noAuctionDatetime()
                .insert();
        testData.event(auctionId, "first_seen")
                .newValue("active")
                .insert();

        String html = digestService.render(
                List.of("NH"),
                OffsetDateTime.parse("2026-07-01T00:00:00+00:00"),
                false
        );

        assertTrue(html.contains("8 Cedar Court, Manchester, NH"));
        assertTrue(html.contains("class='tag'>New<"),
                "a first_seen event on its own should render as New");
    }

    @Test
    void render_showsRemovedTag_forDisappearedEvent() {
        long propertyId = testData.property()
                .address("14 Willow Way, Salem, NH")
                .state("NH")
                .insert();
        long auctionId = testData.auction(propertyId).insert();
        testData.event(auctionId, "disappeared")
                .oldValue("active")
                .insert();

        String html = digestService.render(
                List.of("NH"),
                OffsetDateTime.parse("2026-07-01T00:00:00+00:00"),
                false
        );

        assertTrue(html.contains("14 Willow Way, Salem, NH"));
        assertTrue(html.contains("class='tag'>Removed<"),
                "a disappeared event on its own should render as Removed");
    }

    @Test
    void render_suppressesRow_whenNewAndRemovedInSameWindow() {
        long propertyId = testData.property()
                .address("21 Poplar Place, Derry, NH")
                .state("NH")
                .insert();
        long auctionId = testData.auction(propertyId)
                .noAuctionDatetime()
                .insert();
        testData.event(auctionId, "first_seen")
                .newValue("active")
                .insert();
        testData.event(auctionId, "disappeared")
                .oldValue("active")
                .insert();

        String html = digestService.render(
                List.of("NH"),
                OffsetDateTime.parse("2026-07-01T00:00:00+00:00"),
                false
        );

        // Appeared and vanished within the same digest window -- the
        // subscriber never had a real chance to see it, so it should be
        // dropped entirely rather than shown as New, Removed, or both.
        assertFalse(html.contains("21 Poplar Place, Derry, NH"));
        assertTrue(html.contains("No status changes to report this week."));
    }

    // ---- section bucketing: New Listings / Date Changes / Removed --------
    //
    // renderChanges() splits Status Changes into 4 independently-capped
    // subsections (New Listings, Date Changes, Status Changes, Removed)
    // rather than one mixed table. These confirm each event type lands
    // under the right heading, and that headings with nothing to show
    // don't render at all -- not just that the right tag text exists
    // somewhere, which the earlier tag-text tests already cover.
    //
    // Note: the overall section title "<h2>Status Changes</h2>" is
    // always present regardless of bucket contents, so these
    // deliberately don't assert its absence -- only the per-bucket
    // subheadings ("New Listings", "Date Changes", "Removed").

    @Test
    void render_placesFirstSeenEvent_inNewListingsSection() {
        long propertyId = testData.property()
                .address("100 Ash Street, Keene, NH")
                .state("NH")
                .insert();
        long auctionId = testData.auction(propertyId)
                .noAuctionDatetime()
                .insert();
        testData.event(auctionId, "first_seen")
                .newValue("active")
                .insert();

        String html = digestService.render(
                List.of("NH"),
                OffsetDateTime.parse("2026-07-01T00:00:00+00:00"),
                false
        );

        assertTrue(html.contains("New Listings"),
                "a first_seen-only address should render under the New Listings heading");
        assertFalse(html.contains("Date Changes"),
                "no date_change rows exist, so that subheading should not render at all");
        assertFalse(html.contains(">Removed<"),
                "no disappeared rows exist, so that subheading should not render at all");
    }

    @Test
    void render_placesDisappearedEvent_inRemovedSection() {
        long propertyId = testData.property()
                .address("200 Birch Road, Keene, NH")
                .state("NH")
                .insert();
        long auctionId = testData.auction(propertyId).insert();
        testData.event(auctionId, "disappeared")
                .oldValue("active")
                .insert();

        String html = digestService.render(
                List.of("NH"),
                OffsetDateTime.parse("2026-07-01T00:00:00+00:00"),
                false
        );

        assertTrue(html.contains(">Removed<"),
                "a disappeared-only address should render under the Removed heading");
        assertFalse(html.contains("New Listings"),
                "no first_seen rows exist, so that subheading should not render at all");
        assertFalse(html.contains("Date Changes"),
                "no date_change rows exist, so that subheading should not render at all");
    }

    @Test
    void render_placesDateChangeEvent_inDateChangesSection() {
        long propertyId = testData.property()
                .address("300 Cherry Lane, Keene, NH")
                .state("NH")
                .insert();
        long auctionId = testData.auction(propertyId)
                .auctionDatetime("2026-07-20T10:00:00")
                .insert();
        testData.event(auctionId, "date_change")
                .oldValue("2026-07-15T10:00:00")
                .newValue("2026-07-20T10:00:00")
                .insert();

        String html = digestService.render(
                List.of("NH"),
                OffsetDateTime.parse("2026-07-01T00:00:00+00:00"),
                false
        );

        assertTrue(html.contains("Date Changes"),
                "a date_change-only address should render under the Date Changes heading");
        assertFalse(html.contains("New Listings"),
                "no first_seen rows exist, so that subheading should not render at all");
        assertFalse(html.contains(">Removed<"),
                "no disappeared rows exist, so that subheading should not render at all");
    }

    // ---- listing/map links on Status Change rows --------------------
    //
    // Every bucket shares the same changeRow()/changeLinkCell() code
    // path, so exercising it via one bucket (date_change) is
    // representative of all four -- the gating logic doesn't know or
    // care which bucket a row ends up in.

    @Test
    void render_showsListingAndMapLinks_forChangeWithinTwoWeeks() {
        // Relative to the real clock, same pattern as the existing
        // upcoming-auction test -- CHANGE_LINK_WINDOW_DAYS is evaluated
        // against LocalDateTime.now() inside DigestService itself, not
        // a value this test controls directly.
        String auctionDateTime = LocalDateTime.now().withNano(0).plusDays(5).toString();

        long propertyId = testData.property()
                .address("400 Dogwood Drive, Keene, NH")
                .state("NH")
                .latLng(42.9337, -72.2781)
                .insert();
        long auctionId = testData.auction(propertyId)
                .auctionDatetime(auctionDateTime)
                .sourceUrl("https://example.com/listing/9001")
                .insert();
        testData.event(auctionId, "date_change")
                .oldValue("2026-07-10T10:00:00")
                .newValue(auctionDateTime)
                .insert();

        String html = digestService.render(
                List.of("NH"),
                OffsetDateTime.parse("2026-07-01T00:00:00+00:00"),
                false
        );

        assertTrue(html.contains("https://example.com/listing/9001"),
                "expected a View listing link for a change within the 2-week window");
        assertTrue(html.contains("lat=42.9337&amp;lng=-72.2781") || html.contains("lat=42.9337&lng=-72.2781"),
                "expected a View map link built from the property's lat/lng");
    }

    @Test
    void render_omitsListingLinks_forChangeMoreThanTwoWeeksOut() {
        String auctionDateTime = LocalDateTime.now().withNano(0).plusDays(45).toString();

        long propertyId = testData.property()
                .address("500 Elmwood Ave, Keene, NH")
                .state("NH")
                .latLng(42.9337, -72.2781)
                .insert();
        long auctionId = testData.auction(propertyId)
                .auctionDatetime(auctionDateTime)
                .sourceUrl("https://example.com/listing/9002")
                .insert();
        testData.event(auctionId, "date_change")
                .oldValue("2026-07-10T10:00:00")
                .newValue(auctionDateTime)
                .insert();

        String html = digestService.render(
                List.of("NH"),
                OffsetDateTime.parse("2026-07-01T00:00:00+00:00"),
                false
        );

        assertTrue(html.contains("500 Elmwood Ave, Keene, NH"),
                "the row itself should still render -- only the link is gated by the window");
        assertFalse(html.contains("https://example.com/listing/9002"),
                "a change more than 2 weeks out should not get a View listing link");
    }

}