package com.oncoord.auctionscout.web;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.oncoord.auctionscout.subscriber.SubscriberRepository;
import com.oncoord.auctionscout.stripe.StripeWebhookEventRepository;
import com.stripe.exception.SignatureVerificationException;
import com.stripe.exception.StripeException;
import com.stripe.model.Event;
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
 * The three events registered on the Stripe dashboard endpoint should
 * be: checkout.session.completed, customer.subscription.updated, and
 * customer.subscription.deleted. Anything else received is
 * acknowledged (200) and ignored, since Stripe retries on non-2xx and
 * we don't want unrelated event types (e.g. invoice.* if someone later
 * enables more events in the dashboard without updating this code)
 * causing retry storms.
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
 */
@RestController
public class StripeWebhookController {

    private static final Logger log = LoggerFactory.getLogger(StripeWebhookController.class);

    private final SubscriberRepository subscribers;
    private final StripeWebhookEventRepository processedEvents;
    private final String webhookSecret;

    public StripeWebhookController(SubscriberRepository subscribers,
                                   StripeWebhookEventRepository processedEvents,
                                   @Value("${auctionscout.stripe.webhook-secret}") String webhookSecret) {
        this.subscribers = subscribers;
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

        try {
            switch (event.getType()) {
                case "checkout.session.completed" -> handleCheckoutCompleted(event);
                case "customer.subscription.updated" -> handleSubscriptionUpdated(event);
                case "customer.subscription.deleted" -> handleSubscriptionDeleted(event);
                default -> log.info("Ignoring unhandled Stripe event type {}", event.getType());
            }
        } catch (StripeException e) {
            log.error("Failed to process Stripe event {} ({})", event.getId(), event.getType(), e);
            return ResponseEntity.status(500).body("Failed to process event");
        }

        processedEvents.markProcessed(event.getId());
        return ResponseEntity.ok().build();
    }

    private void handleCheckoutCompleted(Event event) throws StripeException {
        String sessionId = extractRawId(event);
        if (sessionId == null) {
            log.warn("checkout.session.completed event {} had no session id in its raw payload", event.getId());
            return;
        }

        Session session = Session.retrieve(sessionId);
        String email = session.getClientReferenceId();
        if (email == null) {
            log.warn("checkout.session.completed for session {} had no client_reference_id", sessionId);
            return;
        }

        subscribers.recordStripeSubscription(email, session.getCustomer(), session.getSubscription());
        log.info("Recorded Stripe subscription for {} (customer={}, subscription={})",
                email, session.getCustomer(), session.getSubscription());
    }

    private void handleSubscriptionUpdated(Event event) throws StripeException {
        String subscriptionId = extractRawId(event);
        if (subscriptionId == null) {
            log.warn("customer.subscription.updated event {} had no subscription id in its raw payload",
                    event.getId());
            return;
        }

        Subscription subscription = Subscription.retrieve(subscriptionId);
        subscribers.updateStripeSubscriptionStatus(subscription.getId(), subscription.getStatus());
        log.info("Updated Stripe subscription {} status to {}", subscription.getId(), subscription.getStatus());
    }

    private void handleSubscriptionDeleted(Event event) throws StripeException {
        String subscriptionId = extractRawId(event);
        if (subscriptionId == null) {
            log.warn("customer.subscription.deleted event {} had no subscription id in its raw payload",
                    event.getId());
            return;
        }

        subscribers.findEmailByStripeSubscriptionId(subscriptionId).ifPresentOrElse(
                subscribers::deactivate,
                () -> log.warn("customer.subscription.deleted for {} matched no known subscriber", subscriptionId)
        );
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