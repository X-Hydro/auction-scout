package com.oncoord.auctionscout.subscriber;

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
import java.util.List;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Exercises SubscriberRepository against a real SQLite file, same
 * approach as AuctionScoutTokenStoreTest — a mock JdbcTemplate would
 * only prove the right SQL string was called, not that
 * HAS_ACCESS_CLAUSE's date arithmetic and NULL handling actually work.
 *
 * <p>Trial-window tests set subscription_start_date directly via a raw
 * JdbcTemplate fixture helper rather than going through setStates() +
 * a real clock, so "29 days in" vs "31 days in" boundaries can be
 * tested precisely without sleeping the test thread.
 */
class SubscriberRepositoryTest {

    private static final Path TEST_DB_DIR = Path.of("src/test/db");
    private static final Path DB_PATH = TEST_DB_DIR.resolve("subscriber-repository-test.db");
    private static final long THIRTY_DAYS_MILLIS = 30L * 24 * 60 * 60 * 1000;

    private SingleConnectionDataSource dataSource;
    private JdbcTemplate jdbc;
    private SubscriberRepository repo;

    @BeforeEach
    void setUp() throws IOException, SQLException {
        Files.createDirectories(TEST_DB_DIR);
        Files.deleteIfExists(DB_PATH);

        dataSource = new SingleConnectionDataSource("jdbc:sqlite:" + DB_PATH.toAbsolutePath(), true);
        try (Connection conn = dataSource.getConnection()) {
            ScriptUtils.executeSqlScript(conn, new ClassPathResource("auction-scout-manager.sql"));
        }

        jdbc = new JdbcTemplate(dataSource);
        repo = new SubscriberRepository(jdbc);
    }

    @AfterEach
    void tearDown() throws IOException {
        dataSource.destroy();
        Files.deleteIfExists(DB_PATH);
    }

    /**
     * Builds a verified, is_active=1 subscriber with a session token
     * and a specific subscription_start_date, bypassing setStates() so
     * the trial-window tests can set exact boundary values rather than
     * whatever System.currentTimeMillis() happens to be.
     */
    private String createActiveSubscriber(String email, Long subscriptionStartDate) {
        String token = "tok-" + email;
        jdbc.update(
                "INSERT INTO subscribers (email, created_at, verified_at, is_active, session_token, " +
                        "subscription_start_date, email_alerts_enabled) VALUES (?, ?, ?, 1, ?, ?, 1)",
                email, System.currentTimeMillis(), System.currentTimeMillis(), token, subscriptionStartDate
        );
        return token;
    }

    // ---- findEmailBySessionToken: trial window ----

    @Test
    void findEmailBySessionToken_returnsEmail_whenWithinTrialWindow() {
        String token = createActiveSubscriber("intrial@example.com",
                System.currentTimeMillis() - (10L * 24 * 60 * 60 * 1000)); // 10 days in

        Optional<String> found = repo.findEmailBySessionToken(token);

        assertEquals(Optional.of("intrial@example.com"), found);
    }

    @Test
    void findEmailBySessionToken_returnsEmpty_afterTrialExpired_withNoStripeSubscription() {
        String token = createActiveSubscriber("expired@example.com",
                System.currentTimeMillis() - THIRTY_DAYS_MILLIS - 1000); // 30 days + 1s in

        assertTrue(repo.findEmailBySessionToken(token).isEmpty());
    }

    @Test
    void findEmailBySessionToken_returnsEmpty_whenSubscriptionStartDateIsNull() {
        // Shouldn't happen for a real is_active=1 row post-fix, but
        // HAS_ACCESS_CLAUSE should fail closed rather than open if it
        // ever does.
        String token = createActiveSubscriber("nostart@example.com", null);

        assertTrue(repo.findEmailBySessionToken(token).isEmpty());
    }

    @Test
    void findEmailBySessionToken_returnsEmail_afterTrialExpired_ifStripeSubscriptionActive() {
        String token = createActiveSubscriber("paid@example.com",
                System.currentTimeMillis() - THIRTY_DAYS_MILLIS - 1000);
        repo.recordStripeSubscription("paid@example.com", "cus_123", "sub_123");

        Optional<String> found = repo.findEmailBySessionToken(token);

        assertEquals(Optional.of("paid@example.com"), found);
    }

    @Test
    void findEmailBySessionToken_returnsEmpty_afterTrialExpired_whenStripeStatusIsNotActive() {
        String token = createActiveSubscriber("pastdue@example.com",
                System.currentTimeMillis() - THIRTY_DAYS_MILLIS - 1000);
        repo.recordStripeSubscription("pastdue@example.com", "cus_456", "sub_456");
        repo.updateStripeSubscriptionStatus("sub_456", "past_due");

        assertTrue(repo.findEmailBySessionToken(token).isEmpty());
    }

    @Test
    void findEmailBySessionToken_returnsEmpty_forUnknownToken() {
        assertTrue(repo.findEmailBySessionToken("no-such-token").isEmpty());
    }

    // ---- findActiveWithAlertsEnabled: same gating applies to the weekly send list ----

    @Test
    void findActiveWithAlertsEnabled_excludesTrialExpiredSubscribers() {
        createActiveSubscriber("intrial@example.com",
                System.currentTimeMillis() - (5L * 24 * 60 * 60 * 1000));
        createActiveSubscriber("expired@example.com",
                System.currentTimeMillis() - THIRTY_DAYS_MILLIS - 1000);

        List<SubscriberRepository.ActiveSubscriber> result = repo.findActiveWithAlertsEnabled();

        assertEquals(1, result.size());
        assertEquals("intrial@example.com", result.get(0).email());
    }

    @Test
    void findActiveWithAlertsEnabled_includesPaidSubscribersPastTrial() {
        createActiveSubscriber("paid@example.com",
                System.currentTimeMillis() - THIRTY_DAYS_MILLIS - 1000);
        repo.recordStripeSubscription("paid@example.com", "cus_789", "sub_789");

        List<SubscriberRepository.ActiveSubscriber> result = repo.findActiveWithAlertsEnabled();

        assertEquals(1, result.size());
        assertEquals("paid@example.com", result.get(0).email());
    }

    @Test
    void findActiveWithAlertsEnabled_stillRespectsEmailAlertsEnabledFlag() {
        createActiveSubscriber("intrial@example.com", System.currentTimeMillis());
        repo.setEmailAlertsEnabled("intrial@example.com", false);

        assertTrue(repo.findActiveWithAlertsEnabled().isEmpty());
    }

    // ---- Stripe correlation methods ----

    @Test
    void recordStripeSubscription_setsCustomerSubscriptionAndActiveStatus() {
        createActiveSubscriber("sub@example.com", System.currentTimeMillis());

        repo.recordStripeSubscription("sub@example.com", "cus_abc", "sub_abc");

        assertEquals(Optional.of("sub_abc"), repo.findStripeSubscriptionIdByEmail("sub@example.com"));
        assertEquals(Optional.of("sub@example.com"), repo.findEmailByStripeSubscriptionId("sub_abc"));
    }

    @Test
    void findStripeSubscriptionIdByEmail_isEmpty_beforeAnyCheckout() {
        createActiveSubscriber("neversubscribed@example.com", System.currentTimeMillis());

        assertTrue(repo.findStripeSubscriptionIdByEmail("neversubscribed@example.com").isEmpty());
    }

    @Test
    void findEmailByStripeSubscriptionId_isEmpty_forUnknownSubscriptionId() {
        assertTrue(repo.findEmailByStripeSubscriptionId("sub_does_not_exist").isEmpty());
    }

    @Test
    void updateStripeSubscriptionStatus_updatesByStripeSubscriptionId() {
        createActiveSubscriber("cancelme@example.com",
                System.currentTimeMillis() - THIRTY_DAYS_MILLIS - 1000);
        repo.recordStripeSubscription("cancelme@example.com", "cus_x", "sub_x");
        String token = "tok-cancelme@example.com";

        assertTrue(repo.findEmailBySessionToken(token).isPresent(), "sanity check: active status grants access");

        repo.updateStripeSubscriptionStatus("sub_x", "canceled");

        assertTrue(repo.findEmailBySessionToken(token).isEmpty(), "non-active status should no longer grant access");
    }

    // ---- setStates(): subscription_start_date should be set once, not on every save ----

    @Test
    void setStates_setsSubscriptionStartDate_onFirstCall() {
        createActiveSubscriber("firsttime@example.com", null);

        repo.setStates("firsttime@example.com", List.of("VT"));

        Long startDate = jdbc.queryForObject(
                "SELECT subscription_start_date FROM subscribers WHERE email = ?",
                Long.class, "firsttime@example.com"
        );
        assertTrue(startDate != null && startDate > 0);
    }

    @Test
    void setStates_doesNotOverwriteSubscriptionStartDate_onSubsequentCalls() {
        createActiveSubscriber("repeat@example.com", null);
        repo.setStates("repeat@example.com", List.of("VT"));

        // Force a known, obviously-not-"now" value so a second call
        // overwriting it would be unmistakable.
        jdbc.update("UPDATE subscribers SET subscription_start_date = ? WHERE email = ?",
                12345L, "repeat@example.com");

        repo.setStates("repeat@example.com", List.of("VT", "NH"));

        Long startDate = jdbc.queryForObject(
                "SELECT subscription_start_date FROM subscribers WHERE email = ?",
                Long.class, "repeat@example.com"
        );
        assertEquals(12345L, startDate);
    }

    @Test
    void setStates_throws_whenSubscriberIsNotVerified() {
        // is_active = 0 row, e.g. registered but never clicked the magic link.
        jdbc.update("INSERT INTO subscribers (email, created_at, verified_at, is_active) VALUES (?, ?, NULL, 0)",
                "unverified@example.com", System.currentTimeMillis());

        assertFalse(repo.findEmailBySessionToken("irrelevant").isPresent());
        org.junit.jupiter.api.Assertions.assertThrows(IllegalArgumentException.class,
                () -> repo.setStates("unverified@example.com", List.of("VT")));
    }
}