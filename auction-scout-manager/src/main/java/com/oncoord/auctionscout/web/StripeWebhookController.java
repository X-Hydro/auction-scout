package com.oncoord.auctionscout.web;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.oncoord.auctionscout.invoice.InvoiceRepository;
import com.oncoord.auctionscout.subscriber.SubscriberRepository;
import com.oncoord.auctionscout.stripe.StripeWebhookEventRepository;
import com.stripe.exception.SignatureVerificationException;
import com.stripe.exception.StripeException;
import com.stripe.model.Event;
import com.stripe.model.Invoice;
import com.stripe.model.Subscription;
import com.stripe.model.checkout.Session;
import com.stripe.net.Webhook;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RestController;

/**
 * Receives Stripe's webhook POSTs -- a separate controller from
 * SubscriptionController rather than a third method there, because
 * this endpoint has no session-token auth at all (Stripe can't send
 * one); it's authenticated instead by verifying the Stripe-Signature
 * header against auctionscout.stripe.webhook-secret, which only Stripe
 * and this server know.
 *
 * The four events registered on the Stripe dashboard endpoint should
 * be: checkout.session.completed, customer.subscription.updated,
 * customer.subscription.deleted, and invoice.paid. Anything else
 * received is acknowledged (200) and ignored, since Stripe retries on
 * non-2xx and we don't want unrelated event types causing retry storms.
 *
 * Deliberately does NOT rely on event.getDataObjectDeserializer().
 * getObject()'s typed deserialization of the webhook payload itself --
 * that silently returns Optional.empty() (no exception, nothing
 * logged) when the Stripe API version the event was serialized at
 * doesn't match what this stripe-java release expects, which produces
 * exactly the bug this comment is replacing: a 200 response with the
 * handler having done nothing. Instead, every handler pulls just the
 * ID out of the raw JSON and re-fetches the object via a live retrieve()
 * call, which always uses this SDK's own compiled-in API version and
 * so can't hit that mismatch.
 *
 * Each handler returns the subscriber email it resolved (or null, if
 * none) so handle() can record it on the stripe_webhook_events row --
 * makes "which webhook events touched this customer" a plain query
 * against that table instead of needing to reconstruct it from
 * invoices/subscription state later.
 */
@RestController
public class StripeWebhookController {

    private static final Logger log = LoggerFactory.getLogger(StripeWebhookController.class);

    private final SubscriberRepository subscribers;
    private final InvoiceRepository invoices;
    private final StripeWebhookEventRepository processedEvents;
    private final String webhookSecret;

    public StripeWebhookController(SubscriberRepository subscribers,
                                   InvoiceRepository invoices,
                                   StripeWebhookEventRepository processedEvents,
                                   @Value("${auctionscout.stripe.webhook-secret}") String webhookSecret) {
        this.subscribers = subscribers;
        this.invoices = invoices;
        this.processedEvents = processedEvents;
        this.webhookSecret = webhookSecret;
    }

    @PostMapping("/subscription/webhook")
    public ResponseEntity<?> handle(@RequestBody String payload,
                                    @RequestHeader("Stripe-Signature") String signatureHeader) {
        Event event;
        try {
            event = Webhook.constructEvent(payload, signatureHeader, webhookSecret);
        } catch (SignatureVerificationException e) {
            // Wrong/missing secret, or the payload was tampered with in
            // transit -- reject rather than trust an unverified body.
            return ResponseEntity.status(400).body("Invalid signature");
        }

        if (processedEvents.alreadyProcessed(event.getId())) {
            log.info("Ignoring already-processed Stripe event {} ({})", event.getId(), event.getType());
            return ResponseEntity.ok().build();
        }

        log.info("Processing Stripe event {} ({})", event.getId(), event.getType());

        String resolvedEmail;
        try {
            resolvedEmail = switch (event.getType()) {
                case "checkout.session.completed" -> handleCheckoutCompleted(event);
                case "customer.subscription.updated" -> handleSubscriptionUpdated(event);
                case "customer.subscription.deleted" -> handleSubscriptionDeleted(event);
                case "invoice.paid" -> handleInvoicePaid(event);
                default -> {
                    log.info("Ignoring unhandled Stripe event type {}", event.getType());
                    yield null;
                }
            };
        } catch (StripeException e) {
            log.error("Failed to process Stripe event {} ({})", event.getId(), event.getType(), e);
            return ResponseEntity.status(500).body("Failed to process event");
        }

        processedEvents.markProcessed(event.getId(), event.getType(), resolvedEmail);
        return ResponseEntity.ok().build();
    }

    private String handleCheckoutCompleted(Event event) throws StripeException {
        String sessionId = extractRawId(event);
        if (sessionId == null) {
            log.warn("checkout.session.completed event {} had no session id in its raw payload", event.getId());
            return null;
        }

        Session session = Session.retrieve(sessionId);
        String email = session.getClientReferenceId();
        if (email == null) {
            log.warn("checkout.session.completed for session {} had no client_reference_id", sessionId);
            return null;
        }

        subscribers.recordStripeSubscription(email, session.getCustomer(), session.getSubscription(), "active");
        log.info("Recorded Stripe subscription for {} (customer={}, subscription={})",
                email, session.getCustomer(), session.getSubscription());
        return email;
    }

    private String handleSubscriptionUpdated(Event event) throws StripeException {
        String subscriptionId = extractRawId(event);
        if (subscriptionId == null) {
            log.warn("customer.subscription.updated event {} had no subscription id in its raw payload",
                    event.getId());
            return null;
        }

        Subscription subscription = Subscription.retrieve(subscriptionId);
        subscribers.updateStripeSubscriptionStatus(subscription.getId(), subscription.getStatus());
        log.info("Updated Stripe subscription {} status to {}", subscription.getId(), subscription.getStatus());
        return subscribers.findEmailByStripeSubscriptionId(subscription.getId()).orElse(null);
    }

    private String handleSubscriptionDeleted(Event event) throws StripeException {
        String subscriptionId = extractRawId(event);
        if (subscriptionId == null) {
            log.warn("customer.subscription.deleted event {} had no subscription id in its raw payload",
                    event.getId());
            return null;
        }

        // Resolve the email before deactivate() runs, not after -- once
        // it's deactivated there's nothing that changes about looking
        // it up by subscription id, but resolving first keeps the
        // "did we find a match" log and the returned email consistent
        // with each other in one place, in case that ever needs to
        // change.
        return subscribers.findEmailByStripeSubscriptionId(subscriptionId)
                .map(email -> {
                    subscribers.deactivate(email);
                    return email;
                })
                .orElseGet(() -> {
                    log.warn("customer.subscription.deleted for {} matched no known subscriber", subscriptionId);
                    return null;
                });
    }

    private String handleInvoicePaid(Event event) throws StripeException {
        String invoiceId = extractRawId(event);
        if (invoiceId == null) {
            log.warn("invoice.paid event {} had no invoice id in its raw payload", event.getId());
            return null;
        }

        Invoice invoice = Invoice.retrieve(invoiceId);

        // As of stripe-java 33.x, Invoice no longer has a direct
        // getSubscription() -- it was restructured under getParent()
        // to support other invoice sources (e.g. quotes) alongside
        // subscriptions. getParent() and getSubscriptionDetails() can
        // each be null (a one-off invoice, or a quote-originated one),
        // so this has to be checked at every step rather than chained
        // straight through.
        Invoice.Parent parent = invoice.getParent();
        Invoice.Parent.SubscriptionDetails subscriptionDetails =
                parent != null ? parent.getSubscriptionDetails() : null;
        String subscriptionId = subscriptionDetails != null ? subscriptionDetails.getSubscription() : null;

        if (subscriptionId == null) {
            // Not a subscription invoice (e.g. a one-off charge, or a
            // quote-originated invoice) -- nothing for this app to
            // attribute it to.
            log.info("invoice.paid for {} has no subscription -- skipping (not a subscription invoice)",
                    invoiceId);
            return null;
        }

        String email = subscribers.findEmailByStripeSubscriptionId(subscriptionId).orElse(null);
        if (email == null) {
            log.warn("invoice.paid for {} (subscription {}) matched no known subscriber",
                    invoiceId, subscriptionId);
            return null;
        }

        long amountCents = invoice.getAmountPaid() != null ? invoice.getAmountPaid() : 0L;
        long invoiceDate = invoice.getCreated() != null ? invoice.getCreated() * 1000 : System.currentTimeMillis();

        invoices.recordInvoice(email, invoice.getId(), amountCents, invoice.getStatus(),
                invoiceDate, "AuctionScout subscription", "STRIPE-" + event.getId(), null);
        log.info("Recorded invoice {} for {} (amount={} cents)", invoice.getId(), email, amountCents);
        return email;
    }

    /**
     * Pulls just the "id" field out of the event's raw JSON, sidestepping
     * getDataObjectDeserializer().getObject()'s typed deserialization
     * entirely -- see class javadoc for why. Returns null (rather than
     * throwing) if the raw JSON is missing or malformed, since a
     * malformed payload isn't this app's problem to fail loudly over --
     * we log and move on rather than have a single bad event break the
     * whole webhook endpoint.
     */
    private String extractRawId(Event event) {
        String rawJson = event.getDataObjectDeserializer().getRawJson();
        if (rawJson == null || rawJson.isEmpty()) {
            return null;
        }
        try {
            JsonNode root = new ObjectMapper().readTree(rawJson);
            JsonNode idNode = root.path("id");
            return idNode.isMissingNode() ? null : idNode.asText();
        } catch (Exception e) {
            log.warn("Could not parse raw JSON for event {}", event.getId(), e);
            return null;
        }
    }
}