package com.oncoord.auctionscout.stripe;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.core.io.ClassPathResource;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.SingleConnectionDataSource;
import org.springframework.jdbc.datasource.init.ScriptUtils;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.SQLException;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Same real-SQLite-file approach as SubscriberRepositoryTest — the
 * thing actually worth proving here is that a redelivered event (same
 * event_id twice) doesn't throw, since Stripe will do exactly that on
 * any ambiguous delivery.
 */
class StripeWebhookEventRepositoryTest {

    private static final Path TEST_DB_DIR = Path.of("src/test/db");
    private static final Path DB_PATH = TEST_DB_DIR.resolve("stripe-webhook-event-repository-test.db");

    private SingleConnectionDataSource dataSource;
    private JdbcTemplate jdbc;
    private StripeWebhookEventRepository repo;

    @BeforeEach
    void setUp() throws IOException, SQLException {
        Files.createDirectories(TEST_DB_DIR);
        Files.deleteIfExists(DB_PATH);

        dataSource = new SingleConnectionDataSource("jdbc:sqlite:" + DB_PATH.toAbsolutePath(), true);
        try (Connection conn = dataSource.getConnection()) {
            ScriptUtils.executeSqlScript(conn, new ClassPathResource("auction-scout-manager.sql"));
        }

        jdbc = new JdbcTemplate(dataSource);
        repo = new StripeWebhookEventRepository(jdbc);
    }

    @AfterEach
    void tearDown() throws IOException {
        dataSource.destroy();
        Files.deleteIfExists(DB_PATH);
    }

    @Test
    void alreadyProcessed_returnsFalse_forAnUnseenEvent() {
        assertFalse(repo.alreadyProcessed("evt_never_seen"));
    }

    @Test
    void markProcessed_thenAlreadyProcessed_returnsTrue() {
        repo.markProcessed("evt_123");

        assertTrue(repo.alreadyProcessed("evt_123"));
    }

    @Test
    void markProcessed_isIndependentPerEvent() {
        repo.markProcessed("evt_a");

        assertTrue(repo.alreadyProcessed("evt_a"));
        assertFalse(repo.alreadyProcessed("evt_b"));
    }

    @Test
    void markProcessed_calledTwiceForSameEvent_doesNotThrow() {
        // Simulates a redelivered webhook reaching markProcessed() again
        // (e.g. our 200 got lost in transit on the first delivery) --
        // INSERT OR IGNORE means this must be a silent no-op, not a
        // primary-key constraint violation bubbling up to the caller.
        repo.markProcessed("evt_redelivered");
        repo.markProcessed("evt_redelivered");

        assertTrue(repo.alreadyProcessed("evt_redelivered"));
    }

    // ---- markProcessed(eventId, eventType, email) -- the 3-arg overload
    // StripeWebhookController actually calls, added for the
    // stripe_webhook_events.event_type/email columns ----

    @Test
    void markProcessed_withEventTypeAndEmail_persistsBothColumns() {
        repo.markProcessed("evt_full", "checkout.session.completed", "subscriber@example.com");

        Map<String, Object> row = jdbc.queryForMap(
                "SELECT event_type, email FROM stripe_webhook_events WHERE event_id = ?", "evt_full"
        );

        assertEquals("checkout.session.completed", row.get("event_type"));
        assertEquals("subscriber@example.com", row.get("email"));
    }

    @Test
    void markProcessed_withNullEmail_recordsEventTypeButNullEmail() {
        // Mirrors an ignored/unhandled event type in
        // StripeWebhookController -- no subscriber was resolved, so
        // email is legitimately NULL, not a bug.
        repo.markProcessed("evt_ignored", "charge.succeeded", null);

        Map<String, Object> row = jdbc.queryForMap(
                "SELECT event_type, email FROM stripe_webhook_events WHERE event_id = ?", "evt_ignored"
        );

        assertEquals("charge.succeeded", row.get("event_type"));
        assertNull(row.get("email"));
    }

    @Test
    void markProcessed_singleArgOverload_leavesEventTypeAndEmailNull() {
        // The backward-compatible overload used by earlier tests/callers
        // that don't have an event type or email to record.
        repo.markProcessed("evt_legacy");

        Map<String, Object> row = jdbc.queryForMap(
                "SELECT event_type, email FROM stripe_webhook_events WHERE event_id = ?", "evt_legacy"
        );

        assertNull(row.get("event_type"));
        assertNull(row.get("email"));
    }
}