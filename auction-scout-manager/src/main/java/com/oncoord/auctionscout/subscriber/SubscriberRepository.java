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
                "SELECT id, email FROM subscribers WHERE is_active = 1 AND email_alerts_enabled = 1",
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
     */
    public void deactivate(String email) {
        jdbc.update(
                "UPDATE subscribers SET is_active = 0, session_token = NULL, subscription_end_date = ? " +
                        "WHERE email = ?",
                System.currentTimeMillis(), email
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