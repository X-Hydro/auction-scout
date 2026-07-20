package com.oncoord.auctionscout.invoice;

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

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class InvoiceRepositoryTest {

    private static final Path TEST_DB_DIR = Path.of("src/test/db");
    private static final Path DB_PATH = TEST_DB_DIR.resolve("invoice-repository-test.db");

    private SingleConnectionDataSource dataSource;
    private JdbcTemplate jdbc;
    private InvoiceRepository repo;

    @BeforeEach
    void setUp() throws IOException, SQLException {
        Files.createDirectories(TEST_DB_DIR);
        Files.deleteIfExists(DB_PATH);

        dataSource = new SingleConnectionDataSource("jdbc:sqlite:" + DB_PATH.toAbsolutePath(), true);
        try (Connection conn = dataSource.getConnection()) {
            ScriptUtils.executeSqlScript(conn, new ClassPathResource("auction-scout-manager.sql"));
        }

        jdbc = new JdbcTemplate(dataSource);
        repo = new InvoiceRepository(jdbc);

        // invoices.email has a REFERENCES subscribers(email) -- SQLite
        // doesn't enforce foreign keys by default without PRAGMA
        // foreign_keys=ON, but insert a real subscriber row anyway so
        // this test reflects realistic data rather than relying on
        // that default being off.
        jdbc.update(
                "INSERT INTO subscribers (email, created_at, verified_at, is_active) VALUES (?, ?, ?, 1)",
                "billing-test@example.com", System.currentTimeMillis(), System.currentTimeMillis()
        );
    }

    @AfterEach
    void tearDown() throws IOException {
        dataSource.destroy();
        Files.deleteIfExists(DB_PATH);
    }

    @Test
    void recordInvoice_thenFindByEmail_returnsIt() {
        repo.recordInvoice("billing-test@example.com", "in_abc123", 999L, "paid",
                1_700_000_000_000L, "AuctionScout subscription", "STRIPE-evt_1", "4242");

        List<InvoiceRepository.InvoiceRecord> found = repo.findByEmail("billing-test@example.com");

        assertEquals(1, found.size());
        InvoiceRepository.InvoiceRecord record = found.get(0);
        assertEquals("in_abc123", record.stripeInvoiceId());
        assertEquals(999L, record.amountCents());
        assertEquals("paid", record.status());
        assertEquals("4242", record.paymentLast4());
    }

    @Test
    void findByEmail_returnsEmptyList_whenNoInvoicesExist() {
        assertTrue(repo.findByEmail("nobody@example.com").isEmpty());
    }

    @Test
    void recordInvoice_calledTwiceForSameStripeInvoiceId_doesNotThrow() {
        // Simulates the defensive-second-layer scenario described in
        // InvoiceRepository's javadoc -- event-level dedup should
        // normally prevent this, but a duplicate call here must still
        // be a silent no-op, not a UNIQUE constraint violation.
        repo.recordInvoice("billing-test@example.com", "in_dup", 500L, "paid",
                1_700_000_000_000L, "desc", "ref", null);
        repo.recordInvoice("billing-test@example.com", "in_dup", 500L, "paid",
                1_700_000_000_000L, "desc", "ref", null);

        assertEquals(1, repo.findByEmail("billing-test@example.com").size());
    }

    @Test
    void findByEmail_ordersInvoicesMostRecentFirst() {
        repo.recordInvoice("billing-test@example.com", "in_old", 500L, "paid",
                1_000_000L, "old invoice", "ref-old", null);
        repo.recordInvoice("billing-test@example.com", "in_new", 500L, "paid",
                2_000_000L, "new invoice", "ref-new", null);

        List<InvoiceRepository.InvoiceRecord> found = repo.findByEmail("billing-test@example.com");

        assertEquals(2, found.size());
        assertEquals("in_new", found.get(0).stripeInvoiceId(), "most recent invoice should be first");
        assertEquals("in_old", found.get(1).stripeInvoiceId());
    }
}