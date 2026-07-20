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
 * preference being saved. Checkout and the billing portal live here
 * too since they're all part of the same subscription lifecycle.
 *
 * Stripe is wired up as of this cancel() -- see
 * SubscriberRepository.deactivate() for what actually happens to the
 * row (soft-deactivate, not delete): the same method is now reached
 * both from here (manual cancel) and from StripeWebhookController on a
 * customer.subscription.deleted event, so a subscriber cancelled
 * either from our UI or directly from Stripe's own portal ends up in
 * the same place.
 *
 * billingPortal() is deliberately kept separate from cancel() rather
 * than letting the portal handle cancellation too (Stripe's default
 * portal configuration supports both) -- our own /subscription/cancel
 * button stays the one cancellation path; the portal here is scoped to
 * payment-method updates and invoice history only. Whether the portal
 * configuration in the Stripe Dashboard also exposes a cancel option is
 * a Dashboard setting, not something this code enforces -- worth
 * checking that setting matches this intent.
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

    /**
     * Creates a Stripe Billing Portal session and returns its hosted URL
     * so the frontend can redirect the subscriber there to update their
     * payment method (and view invoice history, which the portal shows
     * by default). Unlike checkout(), this requires a Stripe customer to
     * already exist -- a subscriber who's never completed checkout
     * (still trialing, or lapsed with no payment attempt) has nothing
     * to manage yet, so this returns 400 rather than trying to create a
     * portal session with no customer to scope it to.
     */
    @PostMapping("/subscription/billing-portal")
    public ResponseEntity<?> billingPortal(@RequestHeader("X-Session-Token") String sessionToken)
            throws StripeException {
        Optional<String> email = subscribers.findEmailBySessionToken(sessionToken);
        if (email.isEmpty()) {
            return ResponseEntity.status(401).body(Map.of("error", "Invalid or expired session"));
        }

        Optional<String> stripeCustomerId = subscribers.findStripeCustomerIdByEmail(email.get());
        if (stripeCustomerId.isEmpty()) {
            return ResponseEntity.status(400).body(Map.of(
                    "error", "No billing account on file yet -- subscribe first"
            ));
        }

        com.stripe.param.billingportal.SessionCreateParams params =
                com.stripe.param.billingportal.SessionCreateParams.builder()
                        .setCustomer(stripeCustomerId.get())
                        .setReturnUrl(appBaseUrl + "/preferences.html")
                        .build();

        com.stripe.model.billingportal.Session portalSession =
                com.stripe.model.billingportal.Session.create(params);
        return ResponseEntity.ok(Map.of("url", portalSession.getUrl()));
    }
}