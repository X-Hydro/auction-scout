package com.oncoord.auctionscout.web;

import com.oncoord.auctionscout.invoice.InvoiceRepository;
import com.oncoord.auctionscout.subscriber.SubscriberRepository;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RestController;

import java.time.Instant;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Map;
import java.util.Optional;

/**
 * Read-only payment/invoice history for the logged-in subscriber --
 * an in-app alternative to SubscriptionController.billingPortal(),
 * which redirects to Stripe's own hosted portal for the same
 * information. This reads from InvoiceRepository's local copy
 * (populated by StripeWebhookController.handleInvoicePaid() on every
 * invoice.paid event) rather than calling Stripe live -- faster, no
 * live API dependency at page-load time, and still works for a
 * subscriber whose Stripe customer/subscription has since been
 * cancelled (the local rows aren't cleared by deactivate()).
 *
 * Uses findEmailBySessionToken(), not the Active variant -- payment
 * history is exactly the kind of thing a subscriber might reasonably
 * want to check even if their access has lapsed (SubscriptionController
 * .checkout()/cancel()/resume() use the Active variant instead, since
 * those need to stay reachable specifically because access has
 * lapsed -- see SubscriberRepository.findEmailByActiveSessionToken()'s
 * javadoc. Viewing history has no equivalent need).
 */
@RestController
public class InvoiceController {

    private final SubscriberRepository subscribers;
    private final InvoiceRepository invoices;

    public InvoiceController(SubscriberRepository subscribers, InvoiceRepository invoices) {
        this.subscribers = subscribers;
        this.invoices = invoices;
    }

    @GetMapping("/invoices")
    public ResponseEntity<?> listInvoices(@RequestHeader("X-Session-Token") String sessionToken) {
        Optional<String> email = subscribers.findEmailBySessionToken(sessionToken);
        if (email.isEmpty()) {
            return ResponseEntity.status(401).body(Map.of("error", "Invalid or expired session"));
        }

        List<Map<String, Object>> result = invoices.findByEmail(email.get()).stream()
                .map(inv -> Map.<String, Object>of(
                        "date", epochMillisToIsoDate(inv.invoiceDate()),
                        "amount", inv.amountCents() / 100.0,
                        "status", inv.status() == null ? "" : inv.status(),
                        "description", inv.description() == null ? "" : inv.description()
                ))
                .toList();

        return ResponseEntity.ok(Map.of("invoices", result));
    }

    private static String epochMillisToIsoDate(long epochMillis) {
        return Instant.ofEpochMilli(epochMillis).atZone(ZoneOffset.UTC).toLocalDate().toString();
    }
}