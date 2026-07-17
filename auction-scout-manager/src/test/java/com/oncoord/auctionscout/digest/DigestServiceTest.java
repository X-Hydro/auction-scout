package com.oncoord.auctionscout.digest;

import com.oncoord.auctionscout.notification.NotificationRepository;
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
    // Mirrors the two real, physically separate SQLite files in
    // production -- auctionscout.db (properties/auctions/auction_events,
    // written by the Python pipeline) and auctionscout-manager.db
    // (login_tokens/subscribers/email_notifications, owned by this
    // service). See PropertiesDbConnectionManager's javadoc.
    private static final Path DB_PATH = TEST_DB_DIR.resolve("auctionscout-test.db");
    private static final Path MANAGER_DB_PATH = TEST_DB_DIR.resolve("auctionscout-manager-test.db");
    private static final String TEST_EMAIL = "subscriber@example.com";

    private SingleConnectionDataSource dataSource;
    private SingleConnectionDataSource managerDataSource;
    private PropertyDigestTestData testData;
    private DigestService digestService;
    private PropertiesDbConnectionManager dbManager;
    private JdbcTemplate managerJdbc;


    @BeforeEach
    void setUp(TestInfo testInfo) throws IOException, SQLException {
        Files.createDirectories(TEST_DB_DIR);
        Files.deleteIfExists(DB_PATH);
        Files.deleteIfExists(MANAGER_DB_PATH);

        dataSource = new SingleConnectionDataSource("jdbc:sqlite:" + DB_PATH.toAbsolutePath(), true);
        try (Connection conn = dataSource.getConnection()) {
            // properties/auctions/auction_events schema (written by the
            // Python scraping pipeline). Lives one level up from this
            // module's working directory, as a sibling of
            // auction-scout-manager/ (not inside it), so it can't be
            // loaded via ClassPathResource.
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

        // Second, physically separate database: login_tokens/subscribers/
        // email_notifications -- same schema AuctionScoutTokenStoreTest
        // uses, loaded here for its email_notifications table, which
        // NotificationRepository (and therefore DigestService's
        // "Removed" gating) reads from.
        managerDataSource = new SingleConnectionDataSource(
                "jdbc:sqlite:" + MANAGER_DB_PATH.toAbsolutePath(), true);
        try (Connection conn = managerDataSource.getConnection()) {
            Path managerSchemaPath = Path.of("src/main/resources/auction-scout-manager.sql");
            if (!Files.exists(managerSchemaPath)) {
                throw new IllegalStateException(
                        "Expected auction-scout-manager.sql at " + managerSchemaPath.toAbsolutePath()
                                + " (relative to the test's working directory). "
                                + "If the repo layout or run-configuration working directory changed, "
                                + "update the managerSchemaPath value above.");
            }
            ScriptUtils.executeSqlScript(conn, new FileSystemResource(managerSchemaPath.toFile()));
        }
        managerJdbc = new JdbcTemplate(managerDataSource);

        JdbcTemplate jdbc = new JdbcTemplate(dataSource);
        dbManager = new PropertiesDbConnectionManager(DB_PATH.toString());
        testData = new PropertyDigestTestData(jdbc);
        PropertyDigestRepository repository = new PropertyDigestRepository(dbManager);
        NotificationRepository notifications = new NotificationRepository(managerJdbc);
        // null for both SubscriberRepository and TokenService: render()
        // (the only method every test here calls) touches neither --
        // only renderForSubscriber() does, for the email auto-login
        // links. Same reasoning as the existing SubscriberRepository
        // null, just now applying to the newer TokenService param too.
        digestService = new DigestService(repository, null, notifications, null, "https://oncoord.com");
    }

    @AfterEach
    void closeConnection() {
        // Null-checked deliberately: if setUp() throws partway through
        // (e.g. a schema file not found), later fields never get
        // assigned. Without these guards, @AfterEach would NPE on top
        // of the real failure, and worse, skip destroy()-ing whichever
        // DataSource DID get created -- leaving its SQLite file locked
        // (Windows holds an open file handle) for the next test run.
        if (dbManager != null) {
            dbManager.close();
        }
        if (dataSource != null) {
            dataSource.destroy();
        }
        if (managerDataSource != null) {
            managerDataSource.destroy();
        }
        // DB files deliberately left on disk for inspection -- see class javadoc.
    }

    /**
     * Records an email_notifications row with an explicit sent_at,
     * bypassing NotificationRepository.recordSent() (which always
     * stamps System.currentTimeMillis() -- not usable here, since these
     * tests need sent_at deliberately before or after a specific
     * auction_events.detected_at to exercise DigestService's "was this
     * subscriber ever emailed before the listing disappeared" gate).
     */
    private void recordNotificationSentAt(String email, OffsetDateTime sentAt) {
        managerJdbc.update(
                "INSERT INTO email_notifications (email, notification_type, sent_at) VALUES (?, 'weekly', ?)",
                email, sentAt.toInstant().toEpochMilli());
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
        // Date-only, old -> new, no time and no "date_change:" prefix --
        // see DigestService.formatChangeLabel()/dateOnly().
        assertTrue(html.contains("2026-07-15 → 2026-07-20"),
                "expected old date -> new date, with no time component");
        assertFalse(html.contains("2026-07-20T10:00:00"),
                "the time portion should not appear in a date_change tag");
        assertFalse(html.contains("date_change:"),
                "the raw event_type prefix should not appear -- the Date Changes heading already says what this is");

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
    void render_showsRemovedTag_forDisappearedEvent_whenSubscriberWasEmailedBeforehand() {
        long propertyId = testData.property()
                .address("14 Willow Way, Salem, NH")
                .state("NH")
                .insert();
        long auctionId = testData.auction(propertyId).insert();
        testData.event(auctionId, "disappeared")
                .oldValue("active")
                .detectedAt("2026-07-14T09:00:00.000000+00:00")
                .insert();

        // Subscriber received a digest the day before this listing
        // disappeared -- they had a real chance to see it, so "Removed"
        // is legitimate information, not noise. See
        // NotificationRepository.hasSentBefore().
        recordNotificationSentAt(TEST_EMAIL, OffsetDateTime.parse("2026-07-13T09:00:00+00:00"));

        String html = digestService.render(
                TEST_EMAIL,
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
                .detectedAt("2026-07-14T09:00:00.000000+00:00")
                .insert();
        recordNotificationSentAt(TEST_EMAIL, OffsetDateTime.parse("2026-07-13T09:00:00+00:00"));

        String html = digestService.render(
                TEST_EMAIL,
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

    // ---- Removed gating: notification history --------------------------
    //
    // A listing should only be announced as Removed if this subscriber
    // actually had a chance to see it -- i.e. some email went out to
    // them before the removal was detected. See DigestService's
    // wasRemoved branch and NotificationRepository.hasSentBefore().

    @Test
    void render_showsRemoved_whenListingWasIncludedInPriorWeeksEmail() {
        // The realistic weekly cycle: this listing was already known
        // (first seen well before this week -- outside changesSince, so
        // it's NOT flagged "New" in this digest), last week's email went
        // out and would have included it, and now -- within THIS week's
        // window -- it's disappeared. That's exactly the case "Removed"
        // exists to report: the subscriber had a real chance to see it.
        long propertyId = testData.property()
                .address("12 Winslow Way, Concord, NH")
                .state("NH")
                .firstSeenAt("2026-06-01T08:00:00.000000+00:00")
                .insert();
        long auctionId = testData.auction(propertyId).insert();
        testData.event(auctionId, "first_seen")
                .newValue("active")
                .detectedAt("2026-06-01T08:00:00.000000+00:00") // well before this week's changesSince
                .insert();
        testData.event(auctionId, "disappeared")
                .oldValue("active")
                .detectedAt("2026-07-14T09:00:00.000000+00:00") // inside this week's window
                .insert();
        // "Last week's email" -- sent after the listing was first seen,
        // before it disappeared.
        recordNotificationSentAt(TEST_EMAIL, OffsetDateTime.parse("2026-07-08T09:00:00+00:00"));

        String html = digestService.render(
                TEST_EMAIL,
                List.of("NH"),
                OffsetDateTime.parse("2026-07-08T00:00:00+00:00"), // this week's cutoff
                false
        );

        assertTrue(html.contains(">Removed<"),
                "listing was in a prior email and has now disappeared -- should be reported as Removed");
    }

    @Test
    void render_suppressesRemoved_whenSubscriberWasNeverEmailed() {
        // Same shape as render_showsRemoved_whenListingWasIncludedInPriorWeeksEmail
        // above -- listing was genuinely new at some point (first_seen,
        // outside this window) and has now disappeared -- but this time
        // with ZERO notification history: no digest ever went out to
        // this subscriber, for any reason (brand-new subscriber whose
        // first send hasn't happened yet, a delivery failure, etc). No
        // customer ever saw it, so there's nothing to notify them was
        // removed.
        long propertyId = testData.property()
                .address("9 Larch Lane, Dover, NH")
                .state("NH")
                .firstSeenAt("2026-06-01T08:00:00.000000+00:00")
                .insert();
        long auctionId = testData.auction(propertyId).insert();
        testData.event(auctionId, "first_seen")
                .newValue("active")
                .detectedAt("2026-06-01T08:00:00.000000+00:00")
                .insert();
        testData.event(auctionId, "disappeared")
                .oldValue("active")
                .detectedAt("2026-07-14T09:00:00.000000+00:00")
                .insert();
        // No email_notifications row inserted at all for TEST_EMAIL.

        String html = digestService.render(
                TEST_EMAIL,
                List.of("NH"),
                OffsetDateTime.parse("2026-07-08T00:00:00+00:00"),
                false
        );

        assertFalse(html.contains("9 Larch Lane, Dover, NH"),
                "a subscriber never emailed anything never had a chance to see this listing");
        assertTrue(html.contains("No status changes to report this week."));
    }

    @Test
    void render_suppressesRemoved_whenSubscriberWasOnlyEmailedAfterDisappearance() {
        long propertyId = testData.property()
                .address("18 Sumac Street, Dover, NH")
                .state("NH")
                .insert();
        long auctionId = testData.auction(propertyId).insert();
        testData.event(auctionId, "disappeared")
                .oldValue("active")
                .detectedAt("2026-07-14T09:00:00.000000+00:00")
                .insert();
        // Only email on record is AFTER the listing disappeared --
        // doesn't count as "had a chance to see it".
        recordNotificationSentAt(TEST_EMAIL, OffsetDateTime.parse("2026-07-15T09:00:00+00:00"));

        String html = digestService.render(
                TEST_EMAIL,
                List.of("NH"),
                OffsetDateTime.parse("2026-07-01T00:00:00+00:00"),
                false
        );

        assertFalse(html.contains("18 Sumac Street, Dover, NH"),
                "an email sent after the listing disappeared doesn't establish the subscriber ever saw it");
        assertTrue(html.contains("No status changes to report this week."));
    }

    @Test
    void render_suppressesRemoved_whenNoEmailProvided() {
        // The 3-arg render() overload (no subscriber context) delegates
        // to email=null, which is treated the same as "never emailed" --
        // this exercises that null-email path directly, independent of
        // notification data existing or not.
        long propertyId = testData.property()
                .address("27 Tamarack Trail, Dover, NH")
                .state("NH")
                .insert();
        long auctionId = testData.auction(propertyId).insert();
        testData.event(auctionId, "disappeared")
                .oldValue("active")
                .detectedAt("2026-07-14T09:00:00.000000+00:00")
                .insert();
        recordNotificationSentAt(TEST_EMAIL, OffsetDateTime.parse("2026-07-13T09:00:00+00:00"));

        String html = digestService.render(
                List.of("NH"),
                OffsetDateTime.parse("2026-07-01T00:00:00+00:00"),
                false
        );

        assertFalse(html.contains("27 Tamarack Trail, Dover, NH"),
                "a bare render() call with no subscriber context must never claim a listing as Removed");
    }

    // ---- status_change reclassification: Removed / noise / date_change -
    //
    // Real status_change values aren't a controlled vocabulary (see
    // PropertyDigestRepository's javadoc) -- these confirm the specific
    // reclassification rules in DigestService.isRemovalEvent() /
    // isNoiseStatusChange().

    @Test
    void render_treatsTerminalStatusChange_asRemoved() {
        long propertyId = testData.property()
                .address("33 Juniper Way, Rochester, NH")
                .state("NH")
                .insert();
        long auctionId = testData.auction(propertyId).insert();
        testData.event(auctionId, "status_change")
                .oldValue("active")
                .newValue("third party sale")
                .detectedAt("2026-07-14T09:00:00.000000+00:00")
                .insert();
        recordNotificationSentAt(TEST_EMAIL, OffsetDateTime.parse("2026-07-13T09:00:00+00:00"));

        String html = digestService.render(
                TEST_EMAIL,
                List.of("NH"),
                OffsetDateTime.parse("2026-07-01T00:00:00+00:00"),
                false
        );

        assertTrue(html.contains(">Removed<"),
                "a terminal status_change value (third party sale) should be treated as Removed");
        assertFalse(html.contains("status_change:"),
                "the raw status_change tag should not appear once reclassified as Removed");
    }

    @Test
    void render_treatsCancelledStatusChange_asRemoved_evenWithSpellingVariant() {
        long propertyId = testData.property()
                .address("40 Alder Ave, Rochester, NH")
                .state("NH")
                .insert();
        long auctionId = testData.auction(propertyId).insert();
        testData.event(auctionId, "status_change")
                .oldValue("active")
                .newValue("canceled") // single-L spelling variant
                .detectedAt("2026-07-14T09:00:00.000000+00:00")
                .insert();
        recordNotificationSentAt(TEST_EMAIL, OffsetDateTime.parse("2026-07-13T09:00:00+00:00"));

        String html = digestService.render(
                TEST_EMAIL,
                List.of("NH"),
                OffsetDateTime.parse("2026-07-01T00:00:00+00:00"),
                false
        );

        assertTrue(html.contains(">Removed<"),
                "the terminal-status keyword match is substring-based specifically to catch spelling variants");
    }

    @Test
    void render_suppressesNoiseStatusChange_showsNothing() {
        long propertyId = testData.property()
                .address("52 Basswood Blvd, Rochester, NH")
                .state("NH")
                .insert();
        // No auction_datetime: the default fixture date falls inside
        // findUpcoming()'s real-clock 7-day window, and findUpcoming
        // only excludes a property whose most recent event is literally
        // 'disappeared' -- it has no concept of "noise status_change".
        // Without this, the address would legitimately still show up
        // under "Auctions in the Next 7 Days" regardless of what this
        // test is actually checking (that no CHANGES-section row gets
        // generated), making the assertion below fail for a reason
        // unrelated to what's being tested.
        long auctionId = testData.auction(propertyId).noAuctionDatetime().insert();
        testData.event(auctionId, "status_change")
                .oldValue("postponed")
                .newValue("active")
                .insert();

        String html = digestService.render(
                List.of("NH"),
                OffsetDateTime.parse("2026-07-01T00:00:00+00:00"),
                false
        );

        assertFalse(html.contains("52 Basswood Blvd, Rochester, NH"),
                "a non-terminal status_change (active) is noise, not something a subscriber needs to act on");
        assertTrue(html.contains("No status changes to report this week."));
    }

    @Test
    void render_suppressesAvailableContactAuctioneerStatusChange() {
        long propertyId = testData.property()
                .address("61 Spruce Circle, Rochester, NH")
                .state("NH")
                .insert();
        // See noAuctionDatetime() comment in
        // render_suppressesNoiseStatusChange_showsNothing above -- same
        // reasoning applies here.
        long auctionId = testData.auction(propertyId).noAuctionDatetime().insert();
        testData.event(auctionId, "status_change")
                .oldValue("active")
                .newValue("available - contact auctioneer")
                .insert();

        String html = digestService.render(
                List.of("NH"),
                OffsetDateTime.parse("2026-07-01T00:00:00+00:00"),
                false
        );

        assertFalse(html.contains("61 Spruce Circle, Rochester, NH"));
        assertTrue(html.contains("No status changes to report this week."));
    }

    // ---- New + Date Change combo in the same window ---------------------

    @Test
    void render_showsNewTagAndDateChange_whenBothLandInSameWindow() {
        long propertyId = testData.property()
                .address("70 Hemlock Hill, Somersworth, NH")
                .state("NH")
                .insert();
        long auctionId = testData.auction(propertyId)
                .auctionDatetime("2026-07-25T10:00:00")
                .insert();
        testData.event(auctionId, "first_seen")
                .newValue("active")
                .insert();
        testData.event(auctionId, "date_change")
                .oldValue("2026-07-20T10:00:00")
                .newValue("2026-07-25T10:00:00")
                .insert();

        String html = digestService.render(
                List.of("NH"),
                OffsetDateTime.parse("2026-07-01T00:00:00+00:00"),
                false
        );

        assertTrue(html.contains("Date Changes"),
                "a first_seen+date_change combo should be categorized as Date Changes, not New");
        assertFalse(html.contains("New Listings"),
                "the combo shouldn't ALSO appear under New Listings -- one row, one bucket");
        assertTrue(html.contains("class='tag'>New<"),
                "the New context shouldn't be lost -- it should still be tagged alongside the date change");
        assertTrue(html.contains("2026-07-20 → 2026-07-25"),
                "expected the date-only old->new format");
    }

    // ---- Seasoning gate: SEASONING_WINDOW_DAYS ---------------------------
    //
    // A property is exempt from seasoning only if its auction is within
    // SEASONING_WINDOW_DAYS (7) -- waiting the full window would
    // otherwise risk running past the auction itself. Beyond that, a
    // property needs SEASONING_WINDOW_DAYS of confirmed observation
    // (last_seen_at - first_seen_at) before it's shown at all -- New,
    // Date Change, Removed, and Price Change alike.

    @Test
    void render_suppressesNew_whenFarOutAndUnseasoned() {
        long propertyId = testData.property()
                .address("5 Foxglove Lane, Nashua, NH")
                .state("NH")
                // Zero confirmed history -- first seen and last seen at
                // the exact same instant.
                .firstSeenAt("2026-07-14T08:00:00.000000+00:00")
                .lastSeenAt("2026-07-14T08:00:00.000000+00:00")
                .insert();
        long auctionId = testData.auction(propertyId)
                .auctionDatetime("2026-09-15T10:00:00") // well beyond the 7-day window
                .insert();
        testData.event(auctionId, "first_seen")
                .newValue("active")
                .insert();

        String html = digestService.render(
                List.of("NH"),
                OffsetDateTime.parse("2026-07-01T00:00:00+00:00"),
                false
        );

        assertFalse(html.contains("5 Foxglove Lane, Nashua, NH"),
                "far out and not yet confirmed across the seasoning window -- should not be announced");
        assertTrue(html.contains("No status changes to report this week."));
    }

    @Test
    void render_showsNew_forFarOutListing_onceSeasoned() {
        long propertyId = testData.property()
                .address("18 Marigold Court, Nashua, NH")
                .state("NH")
                // Default fixture firstSeenAt/lastSeenAt are ~44 days
                // apart -- well past the 7-day seasoning bar.
                .insert();
        long auctionId = testData.auction(propertyId)
                .auctionDatetime("2026-09-15T10:00:00")
                .insert();
        testData.event(auctionId, "first_seen")
                .newValue("active")
                .insert();

        String html = digestService.render(
                List.of("NH"),
                OffsetDateTime.parse("2026-07-01T00:00:00+00:00"),
                false
        );

        assertTrue(html.contains("18 Marigold Court, Nashua, NH"),
                "far out, but already confirmed across the seasoning window -- should announce normally");
        assertTrue(html.contains("class='tag'>New<"));
    }

    @Test
    void render_showsNew_forNearTermListing_evenWithZeroHistory() {
        // The near-term exemption exists because SEASONING_WINDOW_DAYS
        // of waiting could otherwise run past the auction itself --
        // confirms that exemption still works regardless of history.
        long propertyId = testData.property()
                .address("9 Periwinkle Place, Nashua, NH")
                .state("NH")
                .firstSeenAt("2026-07-14T08:00:00.000000+00:00")
                .lastSeenAt("2026-07-14T08:00:00.000000+00:00")
                .insert();
        long auctionId = testData.auction(propertyId)
                .auctionDatetime("2026-07-20T10:00:00") // within the 7-day window
                .insert();
        testData.event(auctionId, "first_seen")
                .newValue("active")
                .insert();

        String html = digestService.render(
                List.of("NH"),
                OffsetDateTime.parse("2026-07-01T00:00:00+00:00"),
                false
        );

        assertTrue(html.contains("9 Periwinkle Place, Nashua, NH"),
                "near-term auctions bypass seasoning entirely -- waiting would risk missing the window");
        assertTrue(html.contains("class='tag'>New<"));
    }

    @Test
    void render_suppressesEntireRow_whenFarOutUnseasonedListingAlsoHasDateChange() {
        // Regression guard: the seasoning gate must apply uniformly
        // regardless of which event types are present in this window --
        // a date_change riding alongside first_seen must not bypass it.
        long propertyId = testData.property()
                .address("27 Thistle Way, Nashua, NH")
                .state("NH")
                .firstSeenAt("2026-07-14T08:00:00.000000+00:00")
                .lastSeenAt("2026-07-14T08:00:00.000000+00:00")
                .insert();
        long auctionId = testData.auction(propertyId)
                .auctionDatetime("2026-09-20T10:00:00")
                .insert();
        testData.event(auctionId, "first_seen")
                .newValue("active")
                .insert();
        testData.event(auctionId, "date_change")
                .oldValue("2026-09-10T10:00:00")
                .newValue("2026-09-20T10:00:00")
                .insert();

        String html = digestService.render(
                List.of("NH"),
                OffsetDateTime.parse("2026-07-01T00:00:00+00:00"),
                false
        );

        assertFalse(html.contains("27 Thistle Way, Nashua, NH"),
                "an accompanying date_change must not let an unseasoned, far-out listing bypass the gate");
        assertTrue(html.contains("No status changes to report this week."));
    }

    @Test
    void render_suppressesRemoved_whenFarOutUnseasonedListingDisappearsInLaterWindow() {
        // Regression guard: a still-unseasoned listing suppressed one
        // week must not leak through as "Removed" in a LATER digest
        // window, where its first_seen event is no longer in view.
        // Notification history is deliberately present and valid here
        // (sent before the disappearance) -- if this test failed without
        // the seasoning gate but passed because of the notification
        // gate, that would be a false confirmation. Including valid
        // notification history isolates this as specifically a
        // seasoning-gate test.
        long propertyId = testData.property()
                .address("33 Bramble Ridge, Nashua, NH")
                .state("NH")
                .firstSeenAt("2026-07-14T08:00:00.000000+00:00")
                .lastSeenAt("2026-07-14T08:00:00.000000+00:00")
                .insert();
        long auctionId = testData.auction(propertyId)
                .auctionDatetime("2026-09-25T10:00:00")
                .insert();
        testData.event(auctionId, "first_seen")
                .newValue("active")
                .detectedAt("2026-07-14T09:00:00.000000+00:00") // outside this week's window
                .insert();
        testData.event(auctionId, "disappeared")
                .oldValue("active")
                .detectedAt("2026-07-21T09:00:00.000000+00:00") // inside this week's window
                .insert();
        recordNotificationSentAt(TEST_EMAIL, OffsetDateTime.parse("2026-07-15T09:00:00+00:00"));

        String html = digestService.render(
                TEST_EMAIL,
                List.of("NH"),
                OffsetDateTime.parse("2026-07-15T00:00:00+00:00"),
                false
        );

        assertFalse(html.contains("33 Bramble Ridge, Nashua, NH"),
                "still unseasoned -- should not surface as Removed even in a later digest window with valid notification history");
        assertTrue(html.contains("No status changes to report this week."));
    }

}