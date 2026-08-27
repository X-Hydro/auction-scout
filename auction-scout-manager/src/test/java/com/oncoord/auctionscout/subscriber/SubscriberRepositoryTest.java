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
 * findEmailBySessionToken(). States-cap and weekly-email eligibility
 * are gated on hasActiveAccess(): either a real Stripe subscription
 * (active or trialing -- see hasActiveStripeSubscription()), OR still
 * within TRIAL_WINDOW_MILLIS (30 days) of subscription_start_date --
 * the card-free local trial. Past that window with no Stripe
 * subscription, access drops to the free tier (1 state, no weekly
 * emails). Tests that specifically isolate "no Stripe subscription"
 * behavior use a subscription_start_date well outside the trial
 * window (see TRIAL_EXPIRED_START below) so the trial doesn't mask
 * what they're actually testing.
 */
class SubscriberRepositoryTest {

    private static final Path TEST_DB_DIR = Path.of("src/test/db");
    private static final Path DB_PATH = TEST_DB_DIR.resolve("subscriber-repository-test.db");
    private static final String USER_NAME = "AuctionScout User";

    // Registered well outside the 30-day trial window (see
    // SubscriberRepository.TRIAL_WINDOW_MILLIS), so tests using this
    // are isolating Stripe-only behavior -- the trial can't be masking
    // what they're actually testing.
    private static final long TRIAL_EXPIRED_START =
            System.currentTimeMillis() - (45L * 24 * 60 * 60 * 1000);

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
     * a given subscription_start_date. subscription_start_date drives
     * the card-free trial window (see hasActiveAccess()) but never
     * affects dashboard/preferences access itself (see
     * findEmailBySessionToken()). Most tests pass
     * System.currentTimeMillis() (fresh registration, within the trial);
     * tests isolating Stripe-only behavior use TRIAL_EXPIRED_START
     * instead.
     */
    private String createActiveSubscriber(String email, Long subscriptionStartDate) {
        String token = "tok-" + email;
        jdbc.update(
                "INSERT INTO subscribers (email, username, created_at, verified_at, is_active, session_token, " +
                        "subscription_start_date, email_alerts_enabled) VALUES (?, ?, ?, ?, 1, ?, ?, 1)",
                email, USER_NAME, System.currentTimeMillis(), System.currentTimeMillis(), token, subscriptionStartDate
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

    // ---- hasActiveAccess: entitlement for the states cap and weekly emails -- Stripe OR the card-free trial ----

    @Test
    void hasActiveAccess_true_duringTrialWindow_withNoStripeSubscription() {
        createActiveSubscriber("trial@example.com", System.currentTimeMillis());

        assertTrue(repo.hasActiveAccess("trial@example.com"));
    }

    @Test
    void hasActiveAccess_false_pastTrialWindow_withNoStripeSubscription() {
        createActiveSubscriber("expired@example.com", TRIAL_EXPIRED_START);

        assertFalse(repo.hasActiveAccess("expired@example.com"));
    }

    @Test
    void hasActiveAccess_false_whenSubscriptionStartDateIsNull() {
        // Shouldn't normally happen for a verified row (see
        // markVerifiedAndIssueSessionToken()), but hasActiveAccess()
        // should fail safe -- no start date to measure a trial from,
        // and no Stripe subscription either.
        createActiveSubscriber("nostart@example.com", null);

        assertFalse(repo.hasActiveAccess("nostart@example.com"));
    }

    @Test
    void hasActiveAccess_true_viaStripeSubscription_evenPastTrialWindow() {
        // A real subscription doesn't expire just because the local
        // trial window would have -- the two checks are independent,
        // combined with OR.
        createActiveSubscriber("longtimepaid@example.com", TRIAL_EXPIRED_START);
        repo.recordStripeSubscription("longtimepaid@example.com", "cus_lt", "sub_lt", "active");

        assertTrue(repo.hasActiveAccess("longtimepaid@example.com"));
    }

    @Test
    void hasActiveAccess_false_pastTrialWindow_andStripeSubscriptionCanceled() {
        createActiveSubscriber("bothexpired@example.com", TRIAL_EXPIRED_START);
        repo.recordStripeSubscription("bothexpired@example.com", "cus_be", "sub_be", "active");
        repo.updateStripeSubscriptionStatus("sub_be", "canceled");

        assertFalse(repo.hasActiveAccess("bothexpired@example.com"));
    }

    // ---- findActiveWithAlertsEnabled: the weekly send list -- the one thing actually gated ----

    @Test
    void findActiveWithAlertsEnabled_excludesSubscribersWhoNeverSubscribed_andAreOutsideTrialWindow() {
        createActiveSubscriber("neversubscribed@example.com", TRIAL_EXPIRED_START);

        assertTrue(repo.findActiveWithAlertsEnabled().isEmpty());
    }

    @Test
    void findActiveWithAlertsEnabled_includesSubscribersWithinTrialWindow_evenWithNoStripeSubscription() {
        // The card-free local trial -- see SubscriberRepository
        // .hasActiveAccess()/TRIAL_WINDOW_MILLIS. Never touched Stripe
        // at all, but registered recently enough to still be eligible.
        createActiveSubscriber("trialonly@example.com", System.currentTimeMillis());

        List<SubscriberRepository.ActiveSubscriber> result = repo.findActiveWithAlertsEnabled();

        assertEquals(1, result.size());
        assertEquals("trialonly@example.com", result.get(0).email());
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
        // Registered outside the trial window -- otherwise a canceled
        // Stripe status wouldn't matter, since the trial alone would
        // still grant eligibility (see the trial-window tests above).
        createActiveSubscriber("lapsed@example.com", TRIAL_EXPIRED_START);
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
        // Outside the trial window -- see comment on
        // findActiveWithAlertsEnabled_excludesPastDueOrCanceledSubscribers.
        String token = createActiveSubscriber("cancelme@example.com", TRIAL_EXPIRED_START);
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
        // Subscribed, not free -- this test saves 2 states on the second
        // call, which only a subscribed subscriber is allowed to do (see
        // the state-limit tests below). Subscription status is
        // incidental to what this test actually checks.
        repo.recordStripeSubscription("repeat@example.com", "cus_repeat", "sub_repeat", "active");
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
        jdbc.update("INSERT INTO subscribers (email, username, created_at, verified_at, is_active) " +
                        "VALUES (?, ?, ?, NULL, 0)",
                "unverified@example.com", USER_NAME, System.currentTimeMillis());

        assertFalse(repo.findEmailBySessionToken("irrelevant").isPresent());
        org.junit.jupiter.api.Assertions.assertThrows(IllegalArgumentException.class,
                () -> repo.setStates("unverified@example.com", List.of("VT")));
    }

    // ---- setStates(): free vs. subscribed state limit ----

    @Test
    void setStates_allowsOneState_forFreeSubscriber() {
        createActiveSubscriber("free@example.com", System.currentTimeMillis());

        repo.setStates("free@example.com", List.of("VT"));

        assertEquals(List.of("VT"), repo.getStates("free@example.com"));
    }

    @Test
    void setStates_throws_whenFreeSubscriberRequestsMoreThanOneState() {
        // Outside the trial window -- otherwise this is no longer a
        // "free" subscriber for setStates()'s purposes, see
        // setStates_allowsMultipleStates_duringTrialWindow below.
        createActiveSubscriber("free@example.com", TRIAL_EXPIRED_START);

        org.junit.jupiter.api.Assertions.assertThrows(IllegalArgumentException.class,
                () -> repo.setStates("free@example.com", List.of("VT", "NH")));
    }

    @Test
    void setStates_allowsMultipleStates_duringTrialWindow_withNoStripeSubscription() {
        // The card-free local trial applies here too -- setStates()
        // uses hasActiveAccess(), not hasActiveStripeSubscription()
        // directly, so a recent registrant gets the full state cap
        // without ever touching Stripe.
        createActiveSubscriber("trialstates@example.com", System.currentTimeMillis());

        repo.setStates("trialstates@example.com", List.of("MA", "NH", "RI"));

        assertEquals(List.of("MA", "NH", "RI"), repo.getStates("trialstates@example.com"));
    }

    @Test
    void setStates_throws_whenPastDueOrCanceledSubscriberRequestsMoreThanOneState() {
        // Someone whose Stripe subscription lapsed should be treated the
        // same as never having subscribed -- see
        // hasActiveStripeSubscription_false_whenPastDueOrCanceled above.
        // Outside the trial window too, same reasoning as the other
        // Stripe-only tests in this file.
        createActiveSubscriber("lapsed@example.com", TRIAL_EXPIRED_START);
        repo.recordStripeSubscription("lapsed@example.com", "cus_lapsed", "sub_lapsed", "active");
        repo.updateStripeSubscriptionStatus("sub_lapsed", "canceled");

        org.junit.jupiter.api.Assertions.assertThrows(IllegalArgumentException.class,
                () -> repo.setStates("lapsed@example.com", List.of("VT", "NH")));
    }

    @Test
    void setStates_allowsUpToMaxStates_forActiveSubscriber() {
        createActiveSubscriber("paid@example.com", System.currentTimeMillis());
        repo.recordStripeSubscription("paid@example.com", "cus_paid", "sub_paid", "active");

        repo.setStates("paid@example.com", List.of("MA", "NH", "RI"));

        assertEquals(List.of("MA", "NH", "RI"), repo.getStates("paid@example.com"));
    }

    @Test
    void setStates_allowsUpToMaxStates_forTrialingSubscriber() {
        // Trialing gets identical access to active -- see
        // hasActiveStripeSubscription_true_whenTrialing above.
        createActiveSubscriber("trialing@example.com", System.currentTimeMillis());
        repo.recordStripeSubscription("trialing@example.com", "cus_trial", "sub_trial", "trialing");

        repo.setStates("trialing@example.com", List.of("MA", "NH", "RI"));

        assertEquals(List.of("MA", "NH", "RI"), repo.getStates("trialing@example.com"));
    }

    @Test
    void setStates_throws_whenSubscribedSubscriberExceedsMaxStates() {
        createActiveSubscriber("paid@example.com", System.currentTimeMillis());
        repo.recordStripeSubscription("paid@example.com", "cus_paid2", "sub_paid2", "active");

        org.junit.jupiter.api.Assertions.assertThrows(IllegalArgumentException.class,
                () -> repo.setStates("paid@example.com", List.of("VT", "NH", "ME", "MA", "CT")));
    }
}