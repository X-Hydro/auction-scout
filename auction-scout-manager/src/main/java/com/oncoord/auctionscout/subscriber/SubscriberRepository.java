package com.oncoord.auctionscout.subscriber;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.security.SecureRandom;
import java.util.List;
import java.util.Optional;

/**
 * Subscriber persistence, including the session_token used for the
 * storageSession pattern (matching oncoord-manager's oncoord_api_key
 * convention: a permanent, non-expiring bearer token stored client-side
 * in sessionStorage and sent back as a request header) and state
 * preferences (max 4 per subscriber, enforced here since SQLite can't
 * express that as a table constraint).
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
    public String markVerifiedAndIssueSessionToken(String email) {
        String token = generateToken();
        jdbc.update(
                "UPDATE subscribers SET verified_at = ?, is_active = 1, session_token = ? WHERE email = ?",
                System.currentTimeMillis(), token, email
        );
        return token;
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

    public Optional<String> findActiveEmail(String email) {
        return jdbc.query(
                "SELECT email FROM subscribers WHERE email = ? AND is_active = 1",
                rs -> rs.next() ? Optional.of(rs.getString("email")) : Optional.empty(),
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