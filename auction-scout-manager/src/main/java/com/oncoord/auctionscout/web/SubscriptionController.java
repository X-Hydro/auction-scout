package com.oncoord.auctionscout.web;

import com.oncoord.auctionscout.subscriber.SubscriberRepository;
import com.stripe.exception.StripeException;
import com.stripe.model.checkout.Session;
import com.stripe.param.checkout.SessionCreateParams;
import com.stripe.param.SubscriptionCancelParams;
import com.stripe.model.Subscription;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;
import java.util.Optional;

/**
 * Cancel-subscription is deliberately its own controller rather than a
 * third method on PreferencesController: it's a destructive, one-way-
 * feeling action with different UX and billing implications, not a
 * preference being saved. Checkout lives here too since it's the other
 * half of the same subscription lifecycle.
 *
 * Stripe is wired up as of this cancel() -- see
 * SubscriberRepository.deactivate() for what actually happens to the
 * row (soft-deactivate, not delete): the same method is now reached
 * both from here (manual cancel) and from StripeWebhookController on a
 * customer.subscription.deleted event, so a subscriber cancelled
 * either from our UI or directly from Stripe's own portal ends up in
 * the same place.
 */
@RestController
public class SubscriptionController {

    private final SubscriberRepository subscribers;
    private final String stripePriceId;
    private final String appBaseUrl;

    public SubscriptionController(SubscriberRepository subscribers,
                                  @Value("${auctionscout.stripe.price-id}") String stripePriceId,
                                  @Value("${auctionscout.app.base-url}") String appBaseUrl) {
        this.subscribers = subscribers;
        this.stripePriceId = stripePriceId;
        this.appBaseUrl = appBaseUrl;
    }

    /**
     * Creates a Stripe Checkout Session for the caller and returns its
     * hosted URL for the frontend to redirect to. No trial is set on
     * the Stripe side -- our own subscription_start_date-based trial
     * (see SubscriberRepository.HAS_ACCESS_CLAUSE) is what's been
     * granting access up to this point, so billing here starts
     * immediately on checkout completion rather than layering a second,
     * Stripe-native trial on top.
     *
     * client_reference_id carries the subscriber's email so
     * StripeWebhookController can attribute the resulting
     * checkout.session.completed event back to a row without needing a
     * pre-created Stripe customer first.
     */
    @PostMapping("/subscription/checkout")
    public ResponseEntity<?> checkout(@RequestHeader("X-Session-Token") String sessionToken) throws StripeException {
        Optional<String> email = subscribers.findEmailBySessionToken(sessionToken);
        if (email.isEmpty()) {
            return ResponseEntity.status(401).body(Map.of("error", "Invalid or expired session"));
        }

        SessionCreateParams params = SessionCreateParams.builder()
                .setMode(SessionCreateParams.Mode.SUBSCRIPTION)
                .setCustomerEmail(email.get())
                .setClientReferenceId(email.get())
                .addLineItem(SessionCreateParams.LineItem.builder()
                        .setPrice(stripePriceId)
                        .setQuantity(1L)
                        .build())
                .setSuccessUrl(appBaseUrl + "/post-login.html?checkout=success")
                .setCancelUrl(appBaseUrl + "/post-login.html?checkout=cancelled")
                .build();

        Session session = Session.create(params);
        return ResponseEntity.ok(Map.of("url", session.getUrl()));
    }

    @PostMapping("/subscription/cancel")
    public ResponseEntity<?> cancel(@RequestHeader("X-Session-Token") String sessionToken) throws StripeException {
        Optional<String> email = subscribers.findEmailBySessionToken(sessionToken);
        if (email.isEmpty()) {
            return ResponseEntity.status(401).body(Map.of("error", "Invalid or expired session"));
        }

        // Still-trialing subscribers who never checked out have no
        // Stripe subscription to cancel -- only call out to Stripe when
        // one is actually on file.
        Optional<String> stripeSubscriptionId = subscribers.findStripeSubscriptionIdByEmail(email.get());
        if (stripeSubscriptionId.isPresent()) {
            Subscription.retrieve(stripeSubscriptionId.get())
                    .cancel(SubscriptionCancelParams.builder().build());
        }

        subscribers.deactivate(email.get());

        return ResponseEntity.ok(Map.of(
                "message", "Your subscription has been cancelled."
        ));
    }
}