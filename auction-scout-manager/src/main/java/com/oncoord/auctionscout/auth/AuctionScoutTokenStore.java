package com.oncoord.auctionscout.auth;

import com.oncoord.auth.common.TokenRecord;
import com.oncoord.auth.common.TokenStore;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;

import java.util.Optional;

/**
 * TokenStore implementation backed by the {@code login_tokens} table in
 * auctionscout-manage.db.
 *
 * <p>Deliberately schema-minimal: no {@code purpose} column. AuctionScout
 * uses a single implicit purpose for every token issued from this store —
 * whatever purpose string TokenService is called with (e.g. "register")
 * is used for hashing/scoping in memory only and is never persisted here.
 * Do not issue two live tokens with different intended purposes for the
 * same email at the same time; this store cannot tell them apart.
 *
 * <p>NOTE: assumes {@code TokenStore} looks like:
 * <pre>
 *   void save(String token, String subject, long createdAtEpochMillis);
 *   Optional&lt;TokenRecord&gt; findUnused(String token);
 *   void markUsed(String token);
 * </pre>
 * reconstructed from the JDBC verification harness built against
 * oncoord-manager's login_tokens table earlier today. If your real
 * oncoord-auth-common interface differs, the method bodies below are
 * still correct — only the {@code @Override} signatures would need to
 * change to match.
 */
@Component
public class AuctionScoutTokenStore implements TokenStore {

    private final JdbcTemplate jdbc;

    public AuctionScoutTokenStore(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    @Override
    public void save(String token, String subject, long createdAtEpochMillis) {
        jdbc.update(
                "INSERT INTO login_tokens (token, email, created_at, used) VALUES (?, ?, ?, 0)",
                token, subject, createdAtEpochMillis
        );
    }

    @Override
    public Optional<TokenRecord> findUnused(String token) {
        return jdbc.query(
                "SELECT email, created_at FROM login_tokens WHERE token = ? AND used = 0",
                rs -> rs.next()
                        ? Optional.of(new TokenRecord(rs.getString("email"), rs.getLong("created_at")))
                        : Optional.empty(),
                token
        );
    }

    @Override
    public void markUsed(String token) {
        jdbc.update("UPDATE login_tokens SET used = 1 WHERE token = ?", token);
    }

    /**
     * Housekeeping — not part of TokenStore, but worth having on a cron
     * or scheduled task so login_tokens doesn't grow unbounded. Deletes
     * tokens older than the given age regardless of used/unused state.
     */
    public int purgeOlderThan(long cutoffEpochMillis) {
        return jdbc.update("DELETE FROM login_tokens WHERE created_at < ?", cutoffEpochMillis);
    }
}