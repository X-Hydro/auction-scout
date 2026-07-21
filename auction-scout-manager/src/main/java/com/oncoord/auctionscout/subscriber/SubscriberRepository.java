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
    private static final SecureRandom RANDOM = new SecureRandom();

    private final JdbcTemplate jdbc;

    public SubscriberRepository(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    /**
     * Whether a subscriber currently has a live Stripe subscription --
     * 'active' (billing) or 'trialing' (Stripe's own 30-day trial,
     * started at Checkout via trial_period_days -- see
     * SubscriptionController.checkout()). Gates the one thing that's
     * actually paid: weekly email notifications (see
     * findActiveWithAlertsEnabled() and hasActiveStripeSubscription()).
     * Dashboard and preferences access do NOT depend on this -- any
     * verified subscriber can use those for free; see
     * findEmailBySessionToken() below.
     */
    private static final String SUBSCRIBED_CLAUSE =
            "stripe_subscription_status IN ('active', 'trialing')";

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
     *
     * Returns empty if no subscriber row matched the email -- e.g. a
     * token issued for an address that was never actually registered
     * (test-send to an arbitrary address, or a stale/tampered token).
     *
     * Also starts the trial clock here (subscription_start_date), via
     * COALESCE so re-verifying doesn't reset it. This has to happen at
     * verification, not at first preferences save: HAS_ACCESS_CLAUSE
     * (see findEmailBySessionToken) requires subscription_start_date to
     * already be set, and preferences save itself requires passing
     * findEmailBySessionToken first -- setting it any later than this
     * makes a fresh subscriber unable to ever reach the one action that
     * would set it, a real deadlock this fixes.
     */
    public Optional<String> markVerifiedAndIssueSessionToken(String email) {
        String token = generateToken();
        long now = System.currentTimeMillis();
        int rowsUpdated = jdbc.update(
                "UPDATE subscribers SET verified_at = ?, is_active = 1, session_token = ?, " +
                        "subscription_end_date = NULL, subscription_start_date = COALESCE(subscription_start_date, ?) " +
                        "WHERE email = ?",
                now, token, now, email
        );
        return rowsUpdated > 0 ? Optional.of(token) : Optional.empty();
    }

    /**
     * Empty if the token doesn't match a verified, still-active row.
     * Deliberately does NOT check SUBSCRIBED_CLAUSE -- the dashboard and
     * preferences page are free for any verified subscriber; only the
     * weekly emails require an actual (trialing-or-paid) subscription.
     * Same query as findEmailByActiveSessionToken() below now -- kept as
     * two methods since callers express different intent, but they're
     * equivalent today.
     */
    public Optional<String> findEmailBySessionToken(String sessionToken) {
        if (sessionToken == null || sessionToken.isBlank()) {
            return Optional.empty();
        }
        return jdbc.query(
                "SELECT email FROM subscribers WHERE session_token = ? AND is_active = 1",
                rs -> rs.next() ? Optional.of(rs.getString("email")) : Optional.empty(),
                sessionToken
        );
    }

    /**
     * Deliberately looser than findEmailBySessionToken() -- no
     * HAS_ACCESS_CLAUSE check, just "is this a real, still-registered
     * (not explicitly cancelled) subscriber." Used only by billing
     * endpoints (SubscriptionController.checkout(), cancel()) that must
     * stay reachable even once a subscriber's trial has lapsed --
     * otherwise a lapsed-trial subscriber gets locked out of the one
     * endpoint whose entire purpose is letting them pay to regain
     * access, since HAS_ACCESS_CLAUSE would reject them there too.
     *
     * Safe to be looser here: creating a Checkout Session doesn't grant
     * access by itself -- access is only ever granted by the
     * checkout.session.completed webhook actually completing, which is
     * the real gate. An explicitly cancelled subscriber (is_active = 0)
     * still can't reach this at all, since deactivate() clears
     * session_token to NULL -- they have no token left to send until
     * they register again.
     */
    public Optional<String> findEmailByActiveSessionToken(String sessionToken) {
        if (sessionToken == null || sessionToken.isBlank()) {
            return Optional.empty();
        }
        return jdbc.query(
                "SELECT email FROM subscribers WHERE session_token = ? AND is_active = 1",
                rs -> rs.next() ? Optional.of(rs.getString("email")) : Optional.empty(),
                sessionToken
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
     * who's paused emails without cancelling outright; SUBSCRIBED_CLAUSE
     * excludes anyone who's never subscribed (or whose trial/subscription
     * has actually ended in Stripe) -- the dashboard is free, but emails
     * are the paid feature.
     */
    public List<ActiveSubscriber> findActiveWithAlertsEnabled() {
        return jdbc.query(
                "SELECT id, email FROM subscribers WHERE is_active = 1 AND email_alerts_enabled = 1 " +
                        "AND " + SUBSCRIBED_CLAUSE,
                (rs, rowNum) -> new ActiveSubscriber(rs.getInt("id"), rs.getString("email"))
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
     *
     * Also clears stripe_subscription_status here, not just is_active/
     * session_token/subscription_end_date -- without this, a cancelled
     * subscriber's row still shows stripe_subscription_status='active'
     * forever, and HAS_ACCESS_CLAUSE checks that column FIRST, before
     * the trial window. That combination meant a subscriber who
     * genuinely cancelled could get permanent free access back just by
     * re-verifying their email later -- no payment required. Reached
     * from both the manual cancel() flow and the
     * customer.subscription.deleted webhook, so both paths get this
     * fix for free.
     */
    public void deactivate(String email) {
        jdbc.update(
                "UPDATE subscribers SET is_active = 0, session_token = NULL, subscription_end_date = ?, " +
                        "stripe_subscription_status = 'canceled' WHERE email = ?",
                System.currentTimeMillis(), email
        );
    }

    /**
     * Used by PreferencesController to decide whether the frontend
     * should show "Subscribe" or "Cancel" (and now, whether to show the
     * email alerts toggle at all) -- true for 'trialing' as well as
     * 'active', so a subscriber mid-Stripe-trial sees the same UI as a
     * fully paid one, not the pre-subscribe state.
     */
    public boolean hasActiveStripeSubscription(String email) {
        String status = jdbc.query(
                "SELECT stripe_subscription_status FROM subscribers WHERE email = ?",
                rs -> rs.next() ? rs.getString("stripe_subscription_status") : null,
                email
        );
        return "active".equals(status) || "trialing".equals(status);
    }

    /**
     * Called from the checkout.session.completed webhook handler once
     * Stripe confirms checkout. Stores both IDs so a later subscription-
     * lifecycle webhook (which carries the subscription ID, not the
     * subscriber's email) can find its way back to this row via
     * findEmailByStripeSubscriptionId().
     *
     * Takes the real status rather than assuming 'active': with
     * trial_period_days now set on Checkout (see
     * SubscriptionController.checkout()), a subscriber completing
     * checkout is 'trialing', not 'active' -- 'active' only becomes
     * true after the trial ends and the first real charge succeeds. The
     * caller should read the actual status off the Stripe Subscription
     * object rather than hardcode it here.
     */
    public void recordStripeSubscription(String email, String stripeCustomerId, String stripeSubscriptionId, String status) {
        jdbc.update(
                "UPDATE subscribers SET stripe_customer_id = ?, stripe_subscription_id = ?, " +
                        "stripe_subscription_status = ? WHERE email = ?",
                stripeCustomerId, stripeSubscriptionId, status, email
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
    /**
     * Used by SubscriptionController.cancellationInfo() for the
     * "customer since" line on the cancellation confirmation page.
     */
    public Optional<Long> findSubscriptionStartDateByEmail(String email) {
        return jdbc.query(
                "SELECT subscription_start_date FROM subscribers WHERE email = ?",
                rs -> {
                    if (!rs.next()) return Optional.empty();
                    long v = rs.getLong("subscription_start_date");
                    return rs.wasNull() ? Optional.<Long>empty() : Optional.of(v);
                },
                email
        );
    }

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

    /**
     * Used by SubscriptionController.billingPortal() -- a Billing Portal
     * session is scoped to a Stripe customer, not a subscription, so
     * this is looked up separately from findStripeSubscriptionIdByEmail().
     * Empty for a subscriber who's never completed checkout (still
     * trialing, or trial lapsed with no payment attempt) -- there's no
     * Stripe customer record to send them to a portal for yet.
     */
    public Optional<String> findStripeCustomerIdByEmail(String email) {
        return jdbc.query(
                "SELECT stripe_customer_id FROM subscribers WHERE email = ?",
                rs -> rs.next() ? Optional.ofNullable(rs.getString("stripe_customer_id")) : Optional.empty(),
                email
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


    /**
     * Raw Stripe status string ('trialing', 'active', 'canceled', etc.) --
     * needed to pick which of the three subscription-status messages to
     * show on preferences.html. hasActiveStripeSubscription() only
     * returns a boolean, not enough to distinguish the states from each
     * other.
     */
    public Optional<String> findSubscriptionStatusByEmail(String email) {
        return jdbc.query(
                "SELECT stripe_subscription_status FROM subscribers WHERE email = ?",
                rs -> rs.next() ? Optional.ofNullable(rs.getString("stripe_subscription_status")) : Optional.empty(),
                email
        );
    }

    /**
     * Mirrors findSubscriptionStartDateByEmail() -- set by deactivate()
     * when a subscriber cancels. Unlike trial-end/renewal dates, this one
     * doesn't need a Stripe call: it's already the local source of truth
     * for "when does a canceled subscriber's access actually end."
     */
    public Optional<Long> findSubscriptionEndDateByEmail(String email) {
        return jdbc.query(
                "SELECT subscription_end_date FROM subscribers WHERE email = ?",
                rs -> {
                    if (!rs.next()) return Optional.empty();
                    long v = rs.getLong("subscription_end_date");
                    return rs.wasNull() ? Optional.<Long>empty() : Optional.of(v);
                },
                email
        );
    }



}