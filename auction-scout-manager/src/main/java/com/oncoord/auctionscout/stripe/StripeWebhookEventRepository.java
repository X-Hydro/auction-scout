package com.oncoord.auctionscout.stripe;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

/**
 * Tracks which Stripe event IDs StripeWebhookController has already
 * applied, so a redelivered webhook (Stripe retries on anything but a
 * clean 2xx, even if the first delivery actually succeeded on our end)
 * gets acknowledged without being processed twice.
 */
@Repository
public class StripeWebhookEventRepository {

    private final JdbcTemplate jdbc;

    public StripeWebhookEventRepository(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public boolean alreadyProcessed(String eventId) {
        Integer count = jdbc.queryForObject(
                "SELECT COUNT(*) FROM stripe_webhook_events WHERE event_id = ?",
                Integer.class, eventId
        );
        return count != null && count > 0;
    }

    /**
     * INSERT OR IGNORE rather than a plain INSERT: two near-simultaneous
     * deliveries of the same event both reaching this line before either
     * commits shouldn't throw a constraint violation up to the caller --
     * the event is marked processed either way.
     *
     * eventType and email are both nullable: email in particular is
     * only known when a handler actually resolved a subscriber for
     * this event (e.g. checkout.session.completed, invoice.paid) --
     * events StripeWebhookController ignores (the "default" case) have
     * no subscriber attached and record NULL here, which is expected,
     * not a bug.
     */
    public void markProcessed(String eventId, String eventType, String email) {
        jdbc.update(
                "INSERT OR IGNORE INTO stripe_webhook_events (event_id, processed_at, event_type, email) " +
                        "VALUES (?, ?, ?, ?)",
                eventId, System.currentTimeMillis(), eventType, email
        );
    }

    /** Overload for callers that don't have an event type or email to record. */
    public void markProcessed(String eventId) {
        markProcessed(eventId, null, null);
    }
}