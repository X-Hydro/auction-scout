package com.oncoord.auctionscout.auth;

import com.oncoord.auth.common.TokenRecord;
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
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Exercises AuctionScoutTokenStore against a real SQLite file on disk —
 * deliberately not mocking JdbcTemplate. A mock would only prove the
 * right SQL string was called; a real file proves the WHERE clause,
 * constraints, and type coercion actually behave as intended.
 */
class AuctionScoutTokenStoreTest {

    private Path dbPath;
    private SingleConnectionDataSource dataSource;
    private AuctionScoutTokenStore store;

    @BeforeEach
    void setUp() throws IOException, SQLException {
        dbPath = Files.createTempFile("auction-scout-manager-test", ".db");
        // Spring won't run schema init for a hand-built DataSource in a
        // plain unit test (that only happens inside a running Spring
        // context), so apply the real schema.sql explicitly here — same
        // file the app actually uses, no drift between test and prod schema.
        dataSource = new SingleConnectionDataSource("jdbc:sqlite:" + dbPath, true);
        try (Connection conn = dataSource.getConnection()) {
            ScriptUtils.executeSqlScript(conn, new ClassPathResource("auction-scout-manager.sql"));
        }

        store = new AuctionScoutTokenStore(new JdbcTemplate(dataSource));
    }

    @AfterEach
    void tearDown() throws IOException {
        dataSource.destroy();
        Files.deleteIfExists(dbPath);
    }

    @Test
    void save_thenFindUnused_returnsTheSameEmailAndTimestamp() {
        long createdAt = 1_700_000_000_000L;
        store.save("tok-abc123", "buyer@example.com", createdAt);

        Optional<TokenRecord> found = store.findUnused("tok-abc123");

        assertTrue(found.isPresent());
        assertEquals("buyer@example.com", found.get().email());
        assertEquals(createdAt, found.get().createdAtEpochMillis());
    }

    @Test
    void findUnused_returnsEmpty_whenTokenWasNeverIssued() {
        Optional<TokenRecord> found = store.findUnused("does-not-exist");

        assertTrue(found.isEmpty());
    }

    @Test
    void markUsed_thenFindUnused_returnsEmpty() {
        store.save("tok-one-shot", "buyer@example.com", System.currentTimeMillis());

        store.markUsed("tok-one-shot");

        assertTrue(store.findUnused("tok-one-shot").isEmpty());
    }

    @Test
    void markUsed_onATokenThatWasNeverSaved_doesNotThrow() {
        // UPDATE ... WHERE token = ? matching zero rows is a no-op, not
        // an error — this asserts that stays true rather than regressing
        // into an exception if the implementation ever changes.
        store.markUsed("never-existed");
    }

    @Test
    void twoTokensForDifferentEmails_areIndependent() {
        store.save("tok-1", "alice@example.com", System.currentTimeMillis());
        store.save("tok-2", "bob@example.com", System.currentTimeMillis());

        store.markUsed("tok-1");

        assertTrue(store.findUnused("tok-1").isEmpty(), "tok-1 was marked used");
        assertTrue(store.findUnused("tok-2").isPresent(), "tok-2 should be untouched");
        assertEquals("bob@example.com", store.findUnused("tok-2").get().email());
    }

    @Test
    void purgeOlderThan_deletesOnlyTokensOlderThanCutoff() {
        long old = 1_000_000L;
        long recent = 2_000_000L;
        long cutoff = 1_500_000L;
        store.save("tok-old", "old@example.com", old);
        store.save("tok-recent", "recent@example.com", recent);

        int deleted = store.purgeOlderThan(cutoff);

        assertEquals(1, deleted);
        assertTrue(store.findUnused("tok-old").isEmpty());
        assertTrue(store.findUnused("tok-recent").isPresent());
    }

    @Test
    void purgeOlderThan_deletesEvenUsedTokens() {
        // Purge is a cleanup sweep, not a validity check — a used token
        // past the cutoff should still be deleted regardless of its
        // "used" flag.
        store.save("tok-used-and-old", "old@example.com", 1_000_000L);
        store.markUsed("tok-used-and-old");

        int deleted = store.purgeOlderThan(1_500_000L);

        assertEquals(1, deleted);
    }

    @Test
    void save_rejectsDuplicateToken_becauseTokenIsThePrimaryKey() {
        store.save("tok-dup", "first@example.com", System.currentTimeMillis());

        assertThrowsOnDuplicateInsert();
    }

    private void assertThrowsOnDuplicateInsert() {
        try {
            store.save("tok-dup", "second@example.com", System.currentTimeMillis());
            assertFalse(true, "expected a primary key violation on duplicate token");
        } catch (org.springframework.jdbc.UncategorizedSQLException expected) {
            // Not DataIntegrityViolationException: Spring's JDBC exception
            // translation relies on a vendor-specific error-code table
            // (sql-error-codes.xml) that covers Oracle/MySQL/Postgres/H2/
            // Derby, but has no entry for SQLite. With no mapping to use,
            // Spring falls back to wrapping the raw SQLiteException in
            // UncategorizedSQLException rather than translating it — this
            // is real observed behavior, not a guess (confirmed by the
            // actual test failure this replaced).
            assertTrue(expected.getMessage().contains("SQLITE_CONSTRAINT_PRIMARYKEY"));
        }
    }
}