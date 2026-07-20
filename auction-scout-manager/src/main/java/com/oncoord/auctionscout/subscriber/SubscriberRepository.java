package com.oncoord.auctionscout.subscriber;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.security.SecureRandom;
import java.util.List;
import java.util.Optional;

/**
 * Subscriber persistence, including the session_token used for the
 * storageSession pattern (matching oncoord-manager's oncoord_api_key
 * convention: a bearer token stored client-side in sessionStorage and
 * sent back as a request header). It has no timed expiry — it stays
 * valid until either explicitly revoked via invalidateSessionToken()
 * (see LogoutController) or rotated by a fresh markVerifiedAndIssue-
 * SessionToken() call. State preferences (max 4 per subscriber) are
 * also enforced here since SQLite can't express that as a table
 * constraint.
 */
@Repository
public class SubscriberRepository {

    private static final int MAX_STATES_PER_SUBSCRIBER = 4;
    private static final long TRIAL_LENGTH_MILLIS = 30L * 24 * 60 * 60 * 1000;
    private static final SecureRandom RANDOM = new SecureRandom();

    private final JdbcTemplate jdbc;

    public SubscriberRepository(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    /**
     * Appended to is_active = 1 anywhere access needs to be trial-or-
     * paid-gated (session lookup, weekly send list): true if either the
     * subscriber is within 30 days of subscription_start_date, or
     * Stripe has told us (via webhook) that a subscription is active.
     * A NULL subscription_start_date (shouldn't happen for is_active=1
     * rows, since it's set alongside verification) is treated as
     * expired rather than open-ended.
     */
    private static final String HAS_ACCESS_CLAUSE =
            "(stripe_subscription_status = 'active' " +
                    "OR (subscription_start_date IS NOT NULL " +
                    "AND ? < subscription_start_date + " + TRIAL_LENGTH_MILLIS + "))";

    public boolean existsByEmail(String email) {
        Integer count = jdbc.queryForObject(
                "SELECT COUNT(*) FROM subscribers WHERE email = ?",
                Integer.class, email
        );
        return count != null && count > 0;
    }

    public void createUnverified(String email) {
        jdbc.update(
                "INSERT INTO subscribers (email, created_at, verified_at, is_active) " +
                        "VALUES (?, ?, NULL, 0)",
                email, System.currentTimeMillis()
        );
    }

    /**
     * Marks the subscriber verified and issues a fresh session token,
     * returning it so the caller (VerifyController) can hand it back to
     * the client in the response body. Called once per /verify success —
     * re-verifying (e.g. clicking an old email again before it expires)
     * rotates the token rather than reusing the old one, which is a
     * small improvement over the strict oncoord-manager pattern: it
     * means an old, possibly-leaked link can't be replayed to recover a
     * still-valid session after a new one's been issued.
     */
    /**
     * Returns empty if no subscriber row matched the email -- e.g. a
     * token issued for an address that was never actually registered
     * (test-send to an arbitrary address, or a stale/tampered token).
     * Previously this generated and returned a token unconditionally,
     * even when the UPDATE affected zero rows -- the caller (verify())
     * would then report success with a session token that was never
     * actually persisted anywhere, which silently fails one step later
     * when that token gets used and matches nothing.
     */
    public Optional<String> markVerifiedAndIssueSessionToken(String email) {
        String token = generateToken();
        int rowsUpdated = jdbc.update(
                "UPDATE subscribers SET verified_at = ?, is_active = 1, session_token = ?, " +
                        "subscription_end_date = NULL WHERE email = ?",
                System.currentTimeMillis(), token, email
        );
        return rowsUpdated > 0 ? Optional.of(token) : Optional.empty();
    }

    /**
     * Empty if the token doesn't match an active row, OR if it matches
     * one whose trial has lapsed with no active Stripe subscription --
     * same "not logged in" response either way, so an expired-trial
     * subscriber gets bounced back to a resubscribe prompt rather than
     * a confusing partial session.
     */
    public Optional<String> findEmailBySessionToken(String sessionToken) {
        if (sessionToken == null || sessionToken.isBlank()) {
            return Optional.empty();
        }
        return jdbc.query(
                "SELECT email FROM subscribers WHERE session_token = ? AND is_active = 1 " +
                        "AND " + HAS_ACCESS_CLAUSE,
                rs -> rs.next() ? Optional.of(rs.getString("email")) : Optional.empty(),
                sessionToken, System.currentTimeMillis()
        );
    }

    /**
     * Explicit, server-side logout. session_token is otherwise
     * permanent and non-expiring by design (see class javadoc) --
     * without this, a leaked token (browser extension, XSS, a shared
     * computer someone forgot to log out of) remains a valid bearer
     * credential forever, since nothing else in this class ever clears
     * it. This is the one path that revokes the credential itself,
     * rather than just the browser's local copy of it.
     *
     * Matches on the token rather than requiring a resolved email
     * first, and is a silent no-op if the token doesn't match any row
     * -- the caller (LogoutController) always responds the same way
     * either way, so this can't be used to probe which tokens are
     * currently valid.
     */
    public void invalidateSessionToken(String sessionToken) {
        jdbc.update("UPDATE subscribers SET session_token = NULL WHERE session_token = ?", sessionToken);
    }

    public Optional<String> findActiveEmail(String email) {
        return jdbc.query(
                "SELECT email FROM subscribers WHERE email = ? AND is_active = 1",
                rs -> rs.next() ? Optional.of(rs.getString("email")) : Optional.empty(),
                email
        );
    }

    public Optional<Integer> findIdByEmail(String email) {
        return jdbc.query(
                "SELECT id FROM subscribers WHERE email = ?",
                rs -> rs.next() ? Optional.of(rs.getInt("id")) : Optional.empty(),
                email
        );
    }

    public record ActiveSubscriber(int id, String email) {}

    /**
     * Who the weekly scheduler should mail. is_active excludes
     * unverified and cancelled subscribers (same flag, see
     * deactivate()'s javadoc); email_alerts_enabled excludes anyone
     * who's paused emails without cancelling outright.
     */
    public List<ActiveSubscriber> findActiveWithAlertsEnabled() {
        return jdbc.query(
                "SELECT id, email FROM subscribers WHERE is_active = 1 AND email_alerts_enabled = 1 " +
                        "AND " + HAS_ACCESS_CLAUSE,
                (rs, rowNum) -> new ActiveSubscriber(rs.getInt("id"), rs.getString("email")),
                System.currentTimeMillis()
        );
    }

    /**
     * Whether weekly digest emails are paused for this subscriber. This
     * is independent of is_active/cancellation -- a subscriber can stay
     * fully active (still able to view /status and /preferences) while
     * simply not wanting the email itself. Defaults true (matches the
     * column's DB default) if the row can't be found, so a caller that
     * already validated the session via findEmailBySessionToken never
     * sees a false negative here.
     */
    public boolean getEmailAlertsEnabled(String email) {
        Boolean enabled = jdbc.query(
                "SELECT email_alerts_enabled FROM subscribers WHERE email = ?",
                rs -> rs.next() ? rs.getBoolean("email_alerts_enabled") : null,
                email
        );
        return enabled == null || enabled;
    }

    public void setEmailAlertsEnabled(String email, boolean enabled) {
        jdbc.update(
                "UPDATE subscribers SET email_alerts_enabled = ? WHERE email = ?",
                enabled ? 1 : 0, email
        );
    }

    /**
     * Soft-deactivation for "cancel subscription". No Stripe integration
     * yet, so this doesn't touch billing -- it flips is_active off (so
     * every is_active-gated method in this class treats the account as
     * gone immediately), revokes the session token, and records
     * subscription_end_date. That column's javadoc already earmarks it
     * for exactly this -- "once Stripe is added, this is where a paid
     * period's end would be recorded" -- so a manual cancel today and a
     * future Stripe webhook cancellation both land in the same place.
     *
     * To come back, they go through /register again. Since the row
     * still exists, existsByEmail() short-circuits createUnverified(),
     * and the next successful /verify flips is_active back to 1 and
     * clears subscription_end_date via markVerifiedAndIssueSessionToken()
     * -- reactivation is just "verify again", not a separate code path.
     */
    public void deactivate(String email) {
        jdbc.update(
                "UPDATE subscribers SET is_active = 0, session_token = NULL, subscription_end_date = ? " +
                        "WHERE email = ?",
                System.currentTimeMillis(), email
        );
    }

    /**
     * Called from the checkout.session.completed webhook handler once
     * Stripe confirms payment. Stores both IDs so a later subscription-
     * lifecycle webhook (which carries the subscription ID, not the
     * subscriber's email) can find its way back to this row via
     * findEmailByStripeSubscriptionId(). Status is set to 'active' here
     * rather than waiting for a follow-up customer.subscription.created
     * event, since checkout.session.completed already implies that.
     */
    public void recordStripeSubscription(String email, String stripeCustomerId, String stripeSubscriptionId) {
        jdbc.update(
                "UPDATE subscribers SET stripe_customer_id = ?, stripe_subscription_id = ?, " +
                        "stripe_subscription_status = 'active' WHERE email = ?",
                stripeCustomerId, stripeSubscriptionId, email
        );
    }

    /**
     * Called from customer.subscription.updated -- just mirrors
     * whatever status string Stripe sent (see HAS_ACCESS_CLAUSE javadoc
     * for why only 'active' grants access; 'past_due', 'unpaid', etc.
     * fall through to the trial-window check, which by this point has
     * usually already expired).
     */
    public void updateStripeSubscriptionStatus(String stripeSubscriptionId, String status) {
        jdbc.update(
                "UPDATE subscribers SET stripe_subscription_status = ? WHERE stripe_subscription_id = ?",
                status, stripeSubscriptionId
        );
    }

    /**
     * Resolves a subscription-lifecycle webhook (which identifies the
     * subscriber only by Stripe IDs) back to the subscriber's email, so
     * the caller can then reuse findEmailBySessionToken-adjacent logic
     * or call deactivate() on a customer.subscription.deleted event.
     */
    /**
     * Used by SubscriptionController.cancel() to decide whether there's
     * a live Stripe subscription that also needs cancelling (paid
     * subscriber) or not (still-trialing subscriber cancelling early --
     * nothing on the Stripe side to touch).
     */
    public Optional<String> findStripeSubscriptionIdByEmail(String email) {
        return jdbc.query(
                "SELECT stripe_subscription_id FROM subscribers WHERE email = ?",
                rs -> rs.next() ? Optional.ofNullable(rs.getString("stripe_subscription_id")) : Optional.empty(),
                email
        );
    }

    public Optional<String> findEmailByStripeSubscriptionId(String stripeSubscriptionId) {
        return jdbc.query(
                "SELECT email FROM subscribers WHERE stripe_subscription_id = ?",
                rs -> rs.next() ? Optional.of(rs.getString("email")) : Optional.empty(),
                stripeSubscriptionId
        );
    }

    public List<String> getStates(String email) {
        return jdbc.queryForList(
                "SELECT s.state FROM subscriber_states s " +
                        "JOIN subscribers sub ON sub.id = s.subscriber_id " +
                        "WHERE sub.email = ? ORDER BY s.state",
                String.class, email
        );
    }

    /**
     * Replaces the subscriber's full state list atomically — simpler and
     * less error-prone for a UI that submits "here's my complete
     * selection" than diffing adds/removes client-side.
     *
     * @throws IllegalArgumentException if more than 4 states are given,
     *         or if the subscriber doesn't exist / isn't verified
     */
    public void setStates(String email, List<String> states) {
        if (states.size() > MAX_STATES_PER_SUBSCRIBER) {
            throw new IllegalArgumentException(
                    "A subscriber may select at most " + MAX_STATES_PER_SUBSCRIBER + " states");
        }

        Integer subscriberId = jdbc.query(
                "SELECT id FROM subscribers WHERE email = ? AND is_active = 1",
                rs -> rs.next() ? rs.getInt("id") : null,
                email
        );
        if (subscriberId == null) {
            throw new IllegalArgumentException("No verified subscriber for email: " + email);
        }

        // COALESCE, not a plain SET: this only needs to fire once, the
        // first time a subscriber ever saves states. Without it, every
        // subsequent preferences edit would push subscription_start_date
        // forward and the trial window would never actually close.
        jdbc.update(
                "UPDATE subscribers SET subscription_start_date = " +
                        "COALESCE(subscription_start_date, ?) WHERE id = ?",
                System.currentTimeMillis(), subscriberId
        );

        jdbc.update("DELETE FROM subscriber_states WHERE subscriber_id = ?", subscriberId);
        for (String state : states) {
            jdbc.update(
                    "INSERT INTO subscriber_states (subscriber_id, state) VALUES (?, ?)",
                    subscriberId, state.toUpperCase()
            );
        }
    }

    // 32 random bytes, hex-encoded — 64 characters, matching the entropy
    // and format of sendLoginLink.js's crypto.randomBytes(32).toString('hex')
    // for consistency across the platform's token formats.
    private static String generateToken() {
        byte[] bytes = new byte[32];
        RANDOM.nextBytes(bytes);
        StringBuilder hex = new StringBuilder(bytes.length * 2);
        for (byte b : bytes) {
            hex.append(String.format("%02x", b));
        }
        return hex.toString();
    }
}