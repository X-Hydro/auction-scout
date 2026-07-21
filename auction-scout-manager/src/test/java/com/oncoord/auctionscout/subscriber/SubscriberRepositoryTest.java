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
 * only prove the right SQL string was called, not that SUBSCRIBED_CLAUSE
 * actually filters correctly.
 *
 * <p>Access model as of this test file: the dashboard and preferences
 * page are free for any verified (is_active=1) subscriber, regardless
 * of subscription_start_date or Stripe status -- see
 * findEmailBySessionToken(). Only weekly emails are gated on actually
 * having a Stripe subscription, active or still in Stripe's own trial
 * -- see hasActiveStripeSubscription() and findActiveWithAlertsEnabled().
 * subscription_start_date is still recorded (see the setStates() tests
 * below) but no longer drives any access decision.
 */
class SubscriberRepositoryTest {

    private static final Path TEST_DB_DIR = Path.of("src/test/db");
    private static final Path DB_PATH = TEST_DB_DIR.resolve("subscriber-repository-test.db");

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
     * Builds a verified, is_active=1 subscriber with a session token and
     * a given subscription_start_date. subscription_start_date no longer
     * drives any access decision -- it's kept here only because the
     * setStates() tests below still exercise it, and because a real
     * verified row always has one set. Most tests just pass
     * System.currentTimeMillis().
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

    // ---- findEmailBySessionToken: dashboard/preferences access is free once verified ----

    @Test
    void findEmailBySessionToken_returnsEmail_regardlessOfSubscriptionStartDate() {
        // subscription_start_date is far in the past -- under the old
        // trial-window model this would have been "expired." It no
        // longer matters at all for dashboard access.
        String token = createActiveSubscriber("longtime@example.com",
                System.currentTimeMillis() - (365L * 24 * 60 * 60 * 1000));

        assertEquals(Optional.of("longtime@example.com"), repo.findEmailBySessionToken(token));
    }

    @Test
    void findEmailBySessionToken_returnsEmail_whenSubscriptionStartDateIsNull() {
        String token = createActiveSubscriber("nostart@example.com", null);

        assertEquals(Optional.of("nostart@example.com"), repo.findEmailBySessionToken(token));
    }

    @Test
    void findEmailBySessionToken_returnsEmail_regardlessOfStripeStatus() {
        // Never subscribed, and never will have -- dashboard access
        // doesn't depend on Stripe at all.
        String token = createActiveSubscriber("neversubscribed@example.com", System.currentTimeMillis());

        assertEquals(Optional.of("neversubscribed@example.com"), repo.findEmailBySessionToken(token));
    }

    @Test
    void findEmailBySessionToken_returnsEmail_evenWhenStripeSubscriptionHasLapsed() {
        String token = createActiveSubscriber("lapsed@example.com", System.currentTimeMillis());
        repo.recordStripeSubscription("lapsed@example.com", "cus_456", "sub_456", "active");
        repo.updateStripeSubscriptionStatus("sub_456", "past_due");

        // Losing email eligibility (see findActiveWithAlertsEnabled tests
        // below) is not the same as losing dashboard access -- only
        // deactivate() (is_active=0) does that.
        assertEquals(Optional.of("lapsed@example.com"), repo.findEmailBySessionToken(token));
    }

    @Test
    void findEmailBySessionToken_returnsEmpty_forUnknownToken() {
        assertTrue(repo.findEmailBySessionToken("no-such-token").isEmpty());
    }

    // ---- hasActiveStripeSubscription: drives Subscribe/Cancel UI and email eligibility ----

    @Test
    void hasActiveStripeSubscription_true_whenActive() {
        createActiveSubscriber("paid@example.com", System.currentTimeMillis());
        repo.recordStripeSubscription("paid@example.com", "cus_1", "sub_1", "active");

        assertTrue(repo.hasActiveStripeSubscription("paid@example.com"));
    }

    @Test
    void hasActiveStripeSubscription_true_whenTrialing() {
        // The 30-day Stripe trial started by SubscriptionController
        // .checkout()'s trial_period_days -- a subscriber in this state
        // has genuinely subscribed and should be treated the same as a
        // paying one everywhere except billing.
        createActiveSubscriber("trialing@example.com", System.currentTimeMillis());
        repo.recordStripeSubscription("trialing@example.com", "cus_2", "sub_2", "trialing");

        assertTrue(repo.hasActiveStripeSubscription("trialing@example.com"));
    }

    @Test
    void hasActiveStripeSubscription_false_whenNeverSubscribed() {
        createActiveSubscriber("neversubscribed@example.com", System.currentTimeMillis());

        assertFalse(repo.hasActiveStripeSubscription("neversubscribed@example.com"));
    }

    @Test
    void hasActiveStripeSubscription_false_whenPastDueOrCanceled() {
        createActiveSubscriber("lapsed@example.com", System.currentTimeMillis());
        repo.recordStripeSubscription("lapsed@example.com", "cus_3", "sub_3", "active");
        repo.updateStripeSubscriptionStatus("sub_3", "past_due");

        assertFalse(repo.hasActiveStripeSubscription("lapsed@example.com"));
    }

    // ---- findActiveWithAlertsEnabled: the weekly send list -- the one thing actually gated ----

    @Test
    void findActiveWithAlertsEnabled_excludesSubscribersWhoNeverSubscribed() {
        createActiveSubscriber("neversubscribed@example.com", System.currentTimeMillis());

        assertTrue(repo.findActiveWithAlertsEnabled().isEmpty());
    }

    @Test
    void findActiveWithAlertsEnabled_includesTrialingSubscribers() {
        createActiveSubscriber("trialing@example.com", System.currentTimeMillis());
        repo.recordStripeSubscription("trialing@example.com", "cus_4", "sub_4", "trialing");

        List<SubscriberRepository.ActiveSubscriber> result = repo.findActiveWithAlertsEnabled();

        assertEquals(1, result.size());
        assertEquals("trialing@example.com", result.get(0).email());
    }

    @Test
    void findActiveWithAlertsEnabled_includesActivePaidSubscribers() {
        createActiveSubscriber("paid@example.com", System.currentTimeMillis());
        repo.recordStripeSubscription("paid@example.com", "cus_5", "sub_5", "active");

        List<SubscriberRepository.ActiveSubscriber> result = repo.findActiveWithAlertsEnabled();

        assertEquals(1, result.size());
        assertEquals("paid@example.com", result.get(0).email());
    }

    @Test
    void findActiveWithAlertsEnabled_excludesPastDueOrCanceledSubscribers() {
        createActiveSubscriber("lapsed@example.com", System.currentTimeMillis());
        repo.recordStripeSubscription("lapsed@example.com", "cus_6", "sub_6", "active");
        repo.updateStripeSubscriptionStatus("sub_6", "canceled");

        assertTrue(repo.findActiveWithAlertsEnabled().isEmpty());
    }

    @Test
    void findActiveWithAlertsEnabled_stillRespectsEmailAlertsEnabledFlag() {
        // Must actually qualify via Stripe status first -- otherwise this
        // would pass even if the alerts flag were ignored entirely,
        // since a never-subscribed row is excluded either way.
        createActiveSubscriber("paid@example.com", System.currentTimeMillis());
        repo.recordStripeSubscription("paid@example.com", "cus_7", "sub_7", "active");
        repo.setEmailAlertsEnabled("paid@example.com", false);

        assertTrue(repo.findActiveWithAlertsEnabled().isEmpty());
    }

    // ---- Stripe correlation methods ----

    @Test
    void recordStripeSubscription_setsCustomerSubscriptionAndStatus() {
        createActiveSubscriber("sub@example.com", System.currentTimeMillis());

        repo.recordStripeSubscription("sub@example.com", "cus_abc", "sub_abc", "trialing");

        assertEquals(Optional.of("sub_abc"), repo.findStripeSubscriptionIdByEmail("sub@example.com"));
        assertEquals(Optional.of("sub@example.com"), repo.findEmailByStripeSubscriptionId("sub_abc"));
        assertTrue(repo.hasActiveStripeSubscription("sub@example.com"));
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
    void updateStripeSubscriptionStatus_affectsEmailEligibility_butNotDashboardAccess() {
        String token = createActiveSubscriber("cancelme@example.com", System.currentTimeMillis());
        repo.recordStripeSubscription("cancelme@example.com", "cus_x", "sub_x", "active");

        assertTrue(repo.hasActiveStripeSubscription("cancelme@example.com"), "sanity check: active status counts");
        assertFalse(repo.findActiveWithAlertsEnabled().isEmpty(), "sanity check: eligible for weekly emails");

        repo.updateStripeSubscriptionStatus("sub_x", "canceled");

        assertFalse(repo.hasActiveStripeSubscription("cancelme@example.com"), "canceled should lose email eligibility");
        assertTrue(repo.findActiveWithAlertsEnabled().isEmpty(), "canceled should drop off the weekly send list");
        assertTrue(repo.findEmailBySessionToken(token).isPresent(), "but dashboard access is unaffected -- it's free");
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