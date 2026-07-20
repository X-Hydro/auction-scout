package com.oncoord.auctionscout.stripe;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Disabled;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.SingleConnectionDataSource;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.security.SecureRandom;
import java.time.Duration;
import java.time.Instant;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.fail;

/**
 * Manual, human-in-the-loop check that the full checkout -> Stripe ->
 * webhook -> DB round trip actually works against a real Stripe test-
 * mode sandbox. This is NOT a CI-safe unit test -- it's @Disabled on
 * purpose and should stay that way when checked in. Un-disable it
 * locally, run it once by hand, then re-disable before committing.
 *
 * <p>It deliberately calls the running app's real HTTP endpoint
 * (POST /subscription/checkout) rather than the Stripe SDK directly,
 * so it's actually exercising SubscriptionController,
 * StripeWebhookController, and SubscriberRepository together -- the
 * thing that actually needs validating, not just whether the Stripe
 * SDK itself works.
 *
 * <p>Prerequisites (all manual, all outside this test):
 * <ol>
 *   <li>application-local.properties has real Stripe test-mode values
 *       for AUCTIONSCOUT_STRIPE_SECRET_KEY, AUCTIONSCOUT_STRIPE_PRICE_ID,
 *       and a fresh AUCTIONSCOUT_STRIPE_WEBHOOK_SECRET copied from the
 *       currently-running `stripe listen` process (that secret changes
 *       every time the listener restarts).</li>
 *   <li>`stripe listen --forward-to http://localhost:7070/subscription/webhook`
 *       is running in a separate terminal.</li>
 *   <li>The app itself is running locally (local profile, port 7070) in
 *       another terminal -- this test does not start it.</li>
 * </ol>
 *
 * <p>What it does, in order:
 * <ol>
 *   <li>Inserts a throwaway verified subscriber directly into the same
 *       SQLite file the running app uses, bypassing registration/
 *       reCAPTCHA/email entirely (same fixture technique as
 *       SubscriberRepositoryTest) -- with subscription_start_date set
 *       to "now" so it passes the trial-window access check.</li>
 *   <li>Calls POST /subscription/checkout for that subscriber and
 *       prints the returned Checkout URL.</li>
 *   <li>Blocks, printing a prompt to open that URL and complete
 *       checkout with a Stripe test card (4242 4242 4242 4242, any
 *       future expiry, any CVC).</li>
 *   <li>Polls the same database for up to 2 minutes for
 *       stripe_subscription_status to become 'active' on that row --
 *       which only happens if checkout.session.completed actually made
 *       it through stripe listen and StripeWebhookController.</li>
 * </ol>
 */
class ManualStripeCheckoutIntegrationTest {

    // Must match whatever the locally running app actually resolves
    // auctionscout.db.path to -- the property default in
    // application.properties, unless AUCTIONSCOUT_DB_PATH is set to
    // something else for your local run.
    private static final String DB_PATH = "./data/auctionscout-manage.db";
    private static final String APP_BASE_URL = "http://localhost:8081";
    private static final Duration POLL_TIMEOUT = Duration.ofMinutes(2);
    private static final Duration POLL_INTERVAL = Duration.ofSeconds(3);

    @Test
    @Disabled("Manual integration test against real Stripe test mode + a running local instance. " +
            "See class javadoc for setup steps. Re-disable before committing.")
    void checkoutSession_completedInBrowser_endsWithActiveSubscriptionInDb() throws Exception {
        String email = "manual-test-" + System.currentTimeMillis() + "@example.com";
        String sessionToken = randomToken();

        SingleConnectionDataSource dataSource =
                new SingleConnectionDataSource("jdbc:sqlite:" + DB_PATH, true);
        JdbcTemplate jdbc = new JdbcTemplate(dataSource);

        try {
            insertVerifiedSubscriber(jdbc, email, sessionToken);

            String checkoutUrl = requestCheckoutUrl(sessionToken);

            System.out.println();
            System.out.println("=================================================================");
            System.out.println("Open this URL and complete checkout with a Stripe test card:");
            System.out.println(checkoutUrl);
            System.out.println("Test card: 4242 4242 4242 4242, any future expiry, any CVC/zip.");
            System.out.println("Waiting up to " + POLL_TIMEOUT.toMinutes() + " minutes for the webhook...");
            System.out.println("=================================================================");
            System.out.println();

            String finalStatus = pollForSubscriptionStatus(jdbc, email);

            assertEquals("active", finalStatus);
        } finally {
            // Best-effort cleanup -- don't let a cleanup failure mask
            // the real test result above.
            try {
                jdbc.update("DELETE FROM subscribers WHERE email = ?", email);
            } catch (Exception ignored) {
                // Non-fatal: worst case, a throwaway row is left behind
                // for manual inspection.
            }
            dataSource.destroy();
        }
    }

    /**
     * Covers the other half of SubscriptionController: cancel(). Runs
     * the same checkout-and-wait-for-webhook sequence as the test
     * above (a paid, active subscription is a prerequisite for
     * exercising the "there's a live Stripe subscription to also
     * cancel" branch in cancel() -- a still-trialing subscriber with no
     * stripe_subscription_id would only exercise the no-op branch),
     * then calls POST /subscription/cancel and checks the LOCAL
     * deactivation only.
     *
     * <p>Deliberately does not re-verify the Stripe side (that the
     * subscription is actually canceled in Stripe's own records) --
     * that would need Stripe.apiKey set in this test's own JVM, which
     * only the running app has via StripeConfig. cancel()'s local
     * deactivate() call is synchronous within the same HTTP request
     * (no webhook wait needed for this assertion), so watching the
     * `stripe listen` terminal for a customer.subscription.deleted
     * event during the run is your visual confirmation of the Stripe
     * side, same as it was for checkout.
     */
    @Test
    @Disabled("Manual integration test against real Stripe test mode + a running local instance. " +
            "See class javadoc for setup steps. Re-disable before committing.")
    void checkoutThenCancel_deactivatesSubscriberLocally() throws Exception {
        String email = "manual-cancel-test-" + System.currentTimeMillis() + "@example.com";
        String sessionToken = randomToken();

        SingleConnectionDataSource dataSource =
                new SingleConnectionDataSource("jdbc:sqlite:" + DB_PATH, true);
        JdbcTemplate jdbc = new JdbcTemplate(dataSource);

        try {
            insertVerifiedSubscriber(jdbc, email, sessionToken);

            String checkoutUrl = requestCheckoutUrl(sessionToken);

            System.out.println();
            System.out.println("=================================================================");
            System.out.println("Open this URL and complete checkout with a Stripe test card:");
            System.out.println(checkoutUrl);
            System.out.println("Test card: 4242 4242 4242 4242, any future expiry, any CVC/zip.");
            System.out.println("Waiting up to " + POLL_TIMEOUT.toMinutes() + " minutes for the webhook...");
            System.out.println("=================================================================");
            System.out.println();

            assertEquals("active", pollForSubscriptionStatus(jdbc, email));

            System.out.println("Subscription active -- now calling POST /subscription/cancel...");
            requestCancel(sessionToken);

            Integer isActive = jdbc.queryForObject(
                    "SELECT is_active FROM subscribers WHERE email = ?", Integer.class, email
            );
            String remainingSessionToken = jdbc.queryForObject(
                    "SELECT session_token FROM subscribers WHERE email = ?", String.class, email
            );

            assertEquals(0, isActive, "is_active should be flipped to 0 by deactivate()");
            assertNull(remainingSessionToken, "session_token should be revoked by deactivate()");

            System.out.println("Local deactivation confirmed. Check the stripe listen terminal for a " +
                    "customer.subscription.deleted event to confirm the Stripe side too.");
        } finally {
            try {
                jdbc.update("DELETE FROM subscribers WHERE email = ?", email);
            } catch (Exception ignored) {
                // Non-fatal.
            }
            dataSource.destroy();
        }
    }

    private void requestCancel(String sessionToken) throws Exception {
        HttpClient client = HttpClient.newHttpClient();
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(APP_BASE_URL + "/subscription/cancel"))
                .header("X-Session-Token", sessionToken)
                .POST(HttpRequest.BodyPublishers.noBody())
                .build();

        HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
        if (response.statusCode() != 200) {
            fail("POST /subscription/cancel returned " + response.statusCode() + ": " + response.body());
        }
    }

    private void insertVerifiedSubscriber(JdbcTemplate jdbc, String email, String sessionToken) {
        long now = System.currentTimeMillis();
        jdbc.update(
                "INSERT INTO subscribers (email, created_at, verified_at, is_active, session_token, " +
                        "subscription_start_date, email_alerts_enabled) VALUES (?, ?, ?, 1, ?, ?, 1)",
                email, now, now, sessionToken, now
        );
    }

    private String requestCheckoutUrl(String sessionToken) throws Exception {
        HttpClient client = HttpClient.newHttpClient();
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(APP_BASE_URL + "/subscription/checkout"))
                .header("X-Session-Token", sessionToken)
                .POST(HttpRequest.BodyPublishers.noBody())
                .build();

        HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
        if (response.statusCode() != 200) {
            fail("POST /subscription/checkout returned " + response.statusCode() + ": " + response.body() +
                    " -- is the app actually running on " + APP_BASE_URL + "?");
        }

        JsonNode body = new ObjectMapper().readTree(response.body());
        return body.get("url").asText();
    }

    private String pollForSubscriptionStatus(JdbcTemplate jdbc, String email) throws InterruptedException {
        Instant deadline = Instant.now().plus(POLL_TIMEOUT);
        String status = null;

        while (Instant.now().isBefore(deadline)) {
            status = jdbc.queryForObject(
                    "SELECT stripe_subscription_status FROM subscribers WHERE email = ?",
                    String.class, email
            );
            if ("active".equals(status)) {
                return status;
            }
            Thread.sleep(POLL_INTERVAL.toMillis());
        }

        fail("Timed out after " + POLL_TIMEOUT.toMinutes() + " minutes waiting for " +
                "stripe_subscription_status to become 'active' (last seen: " + status + "). " +
                "Check that stripe listen is running and forwarding to " +
                APP_BASE_URL + "/subscription/webhook, and that the app's console shows the " +
                "webhook was received without error.");
        return status; // unreachable, fail() throws
    }

    // 32 random bytes, hex-encoded -- matches SubscriberRepository's own
    // token format, though the exact format doesn't matter here since
    // this token is only ever compared against itself.
    private static String randomToken() {
        byte[] bytes = new byte[32];
        new SecureRandom().nextBytes(bytes);
        StringBuilder hex = new StringBuilder(bytes.length * 2);
        for (byte b : bytes) {
            hex.append(String.format("%02x", b));
        }
        return hex.toString();
    }
}