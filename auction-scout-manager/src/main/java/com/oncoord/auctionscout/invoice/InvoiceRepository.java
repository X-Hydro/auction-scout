package com.oncoord.auctionscout.invoice;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.util.List;

/**
 * Local record of what Stripe actually billed a subscriber -- lets a
 * billing question ("did I get charged in March?") be answered from
 * this app's own data instead of the Stripe Dashboard. Populated from
 * the invoice.paid webhook event (see StripeWebhookController).
 */
@Repository
public class InvoiceRepository {

    private final JdbcTemplate jdbc;

    public InvoiceRepository(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    /**
     * INSERT OR IGNORE rather than a plain INSERT: stripe_invoice_id is
     * UNIQUE, and event-level dedup in StripeWebhookEventRepository
     * already prevents this from being called twice for the same
     * event in practice -- this is a defensive second layer, not the
     * primary guard, so a duplicate call is a silent no-op rather than
     * a constraint-violation exception.
     */
    public void recordInvoice(String email, String stripeInvoiceId, long amountCents, String status,
                              long invoiceDate, String description, String paymentReference,
                              String paymentLast4) {
        jdbc.update(
                "INSERT OR IGNORE INTO invoices " +
                        "(email, invoice_date, amount_cents, status, description, created_at, " +
                        "payment_reference, payment_last4, stripe_invoice_id) " +
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                email, invoiceDate, amountCents, status, description, System.currentTimeMillis(),
                paymentReference, paymentLast4, stripeInvoiceId
        );
    }

    public record InvoiceRecord(
            long id, String email, long invoiceDate, long amountCents, String status,
            String description, String paymentReference, String paymentLast4, String stripeInvoiceId
    ) {}

    /** For a support lookup: every invoice on file for one subscriber, most recent first. */
    public List<InvoiceRecord> findByEmail(String email) {
        return jdbc.query(
                "SELECT id, email, invoice_date, amount_cents, status, description, " +
                        "payment_reference, payment_last4, stripe_invoice_id " +
                        "FROM invoices WHERE email = ? ORDER BY invoice_date DESC",
                (rs, rowNum) -> new InvoiceRecord(
                        rs.getLong("id"), rs.getString("email"), rs.getLong("invoice_date"),
                        rs.getLong("amount_cents"), rs.getString("status"), rs.getString("description"),
                        rs.getString("payment_reference"), rs.getString("payment_last4"),
                        rs.getString("stripe_invoice_id")
                ),
                email
        );
    }
}