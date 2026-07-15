package com.oncoord.auctionscout.digest;

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
            // uses. It lives two levels up from wherever this test runs
            // from, as a sibling of the auction-scout-manager module (not
            // inside it), so it can't be loaded via ClassPathResource.
            Path schemaPath = Path.of("../schema.sql");
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
        testData = new PropertyDigestTestData(jdbc);
        PropertyDigestRepository repository = new PropertyDigestRepository(jdbc);
        digestService = new DigestService(repository, null, "https://oncoord.com");
    }

    @AfterEach
    void closeConnection() {
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
                OffsetDateTime.parse("2026-07-01T00:00:00+00:00")
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
                OffsetDateTime.parse("2026-07-01T00:00:00+00:00")
        );

        assertFalse(html.contains("10 Maple Ave, Manchester, NH"));
        assertTrue(html.contains("No status changes to report this week."));
    }

}