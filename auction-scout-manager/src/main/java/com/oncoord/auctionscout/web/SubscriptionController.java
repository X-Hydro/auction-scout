package com.oncoord.auctionscout.web;

import com.oncoord.auctionscout.digest.DigestSendService;
import com.oncoord.auctionscout.notification.NotificationRepository;
import com.oncoord.auctionscout.subscriber.SubscriberRepository;
import com.stripe.exception.StripeException;
import com.stripe.model.Price;
import com.stripe.model.checkout.Session;
import com.stripe.param.checkout.SessionCreateParams;
import com.stripe.param.SubscriptionUpdateParams;
import com.stripe.model.Subscription;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RestController;

import java.util.HashMap;
import java.util.Map;
import java.util.Optional;

/**
 * Cancel-subscription is deliberately its own controller rather than a
 * third method on PreferencesController: it's a destructive, one-way-
 * feeling action with different UX and billing implications, not a
 * preference being saved. Checkout and the billing portal live here
 * too since they're all part of the same subscription lifecycle.
 *
 * cancel() uses Stripe's cancel-at-period-end rather than immediate
 * cancellation for a paid subscriber -- they keep access through what
 * they already paid for. Access only actually gets revoked later, when
 * Stripe's own customer.subscription.deleted webhook fires at the real
 * period end (StripeWebhookController already handles that event
 * correctly; this class doesn't need to know when that happens). A
 * still-trialing subscriber with no Stripe subscription at all is the
 * one case that still deactivates immediately here -- there's no paid
 * period to honor for them.
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
    private final NotificationRepository notifications;
    private final String stripePriceId;
    private final String appBaseUrl;

    public SubscriptionController(SubscriberRepository subscribers,
                                  NotificationRepository notifications,
                                  @Value("${auctionscout.stripe.price-id}") String stripePriceId,
                                  @Value("${auctionscout.app.base-url}") String appBaseUrl) {
        this.subscribers = subscribers;
        this.notifications = notifications;
        this.stripePriceId = stripePriceId;
        this.appBaseUrl = appBaseUrl;
    }

    /**
     * Creates a Stripe Checkout Session for the caller and returns its
     * hosted URL for the frontend to redirect to. trial_period_days=30
     * gives Stripe its own 30-day trial: a card is collected now, but
     * nothing is charged until the trial ends, and the subscriber can
     * cancel any time before then with nothing paid. This is the one
     * thing access is actually gated on -- the dashboard itself is free
     * for any verified subscriber (see SubscriberRepository
     * .findEmailBySessionToken()); only weekly emails require this.
     *
     * client_reference_id carries the subscriber's email so
     * StripeWebhookController can attribute the resulting
     * checkout.session.completed event back to a row without needing a
     * pre-created Stripe customer first.
     */
    @PostMapping("/subscription/checkout")
    public ResponseEntity<?> checkout(@RequestHeader("X-Session-Token") String sessionToken) throws StripeException {
        // findEmailByActiveSessionToken(), not findEmailBySessionToken():
        // this must stay reachable even after a subscriber's trial has
        // lapsed -- that's the whole point of this endpoint. See its
        // javadoc in SubscriberRepository.
        Optional<String> email = subscribers.findEmailByActiveSessionToken(sessionToken);
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
                .setSubscriptionData(SessionCreateParams.SubscriptionData.builder()
                        .setTrialPeriodDays(30L)
                        .build())
                .setSuccessUrl(appBaseUrl + "/auction-scout/preferences.html?checkout=success")
                .setCancelUrl(appBaseUrl + "/auction-scout/preferences.html?checkout=cancelled")
                .build();

        Session session = Session.create(params);
        return ResponseEntity.ok(Map.of("url", session.getUrl()));
    }

    private volatile Map<String, Object> cachedPriceInfo;
    private volatile long priceCacheExpiresAt;

    /**
     * Public, unauthenticated -- price isn't subscriber-specific. Cached
     * for an hour since it changes essentially never and Stripe rate-
     * limits; avoids a live Stripe call on every preferences.html load.
     */
    @GetMapping("/subscription/price-info")
    public ResponseEntity<?> priceInfo() throws StripeException {
        long now = System.currentTimeMillis();
        if (cachedPriceInfo == null || now > priceCacheExpiresAt) {
            Price price = Price.retrieve(stripePriceId);
            cachedPriceInfo = Map.of(
                    "amount", price.getUnitAmount(),
                    "currency", price.getCurrency(),
                    "interval", price.getRecurring().getInterval()
            );
            priceCacheExpiresAt = now + 3_600_000; // 1 hour
        }
        return ResponseEntity.ok(cachedPriceInfo);
    }

    /**
     * Read-only preview for the cancellation confirmation page --
     * doesn't cancel anything, just gathers what that page needs to
     * show before the subscriber actually confirms. activeUntil means
     * two different things depending on whether there's a real Stripe
     * subscription: for a paid subscriber it's the real, live
     * current_period_end fetched from Stripe (fetched fresh here, not
     * cached locally, since it changes every billing cycle); for a
     * still-trialing subscriber with nothing to cancel on Stripe's
     * side, cancelling is immediate, so it's just "now."
     */
    @GetMapping("/subscription/cancellation-info")
    public ResponseEntity<?> cancellationInfo(@RequestHeader("X-Session-Token") String sessionToken)
            throws StripeException {
        Optional<String> email = subscribers.findEmailByActiveSessionToken(sessionToken);
        if (email.isEmpty()) {
            return ResponseEntity.status(401).body(Map.of("error", "Invalid or expired session"));
        }

        Map<String, Object> info = new HashMap<>();
        info.put("customerSince", subscribers.findSubscriptionStartDateByEmail(email.get()).orElse(null));
        info.put("notificationsSent", notifications.countSentByType(email.get(), DigestSendService.TYPE_WEEKLY));

        Optional<String> stripeSubscriptionId = subscribers.findStripeSubscriptionIdByEmail(email.get());
        if (stripeSubscriptionId.isPresent() && subscribers.hasActiveStripeSubscription(email.get())) {
            Subscription subscription = Subscription.retrieve(stripeSubscriptionId.get());
            // As of stripe-java 33.x, current_period_end lives on each
            // subscription item, not on the Subscription itself (same
            // restructuring as Invoice.getSubscription() -- see
            // StripeWebhookController). A single-price subscription
            // like this app's always has exactly one item.
            long currentPeriodEndSeconds = subscription.getItems().getData().get(0).getCurrentPeriodEnd();
            info.put("activeUntil", currentPeriodEndSeconds * 1000);
            info.put("immediateCancellation", false);
        } else {
            info.put("activeUntil", System.currentTimeMillis());
            info.put("immediateCancellation", true);
        }

        return ResponseEntity.ok(info);
    }

    @PostMapping("/subscription/cancel")
    public ResponseEntity<?> cancel(@RequestHeader("X-Session-Token") String sessionToken) throws StripeException {
        Optional<String> email = subscribers.findEmailByActiveSessionToken(sessionToken);
        if (email.isEmpty()) {
            return ResponseEntity.status(401).body(Map.of("error", "Invalid or expired session"));
        }

        // Still-trialing subscribers who never checked out have no
        // Stripe subscription to cancel -- and nothing paid to honor,
        // so they deactivate immediately below. A paid subscriber
        // schedules cancellation for period end instead and keeps
        // access until then -- deactivate() is NOT called here for
        // that case; StripeWebhookController's customer.subscription.
        // deleted handler does it later, when Stripe actually ends the
        // subscription.
        Optional<String> stripeSubscriptionId = subscribers.findStripeSubscriptionIdByEmail(email.get());
        if (stripeSubscriptionId.isPresent() && subscribers.hasActiveStripeSubscription(email.get())) {
            Subscription.retrieve(stripeSubscriptionId.get())
                    .update(SubscriptionUpdateParams.builder()
                            .setCancelAtPeriodEnd(true)
                            .build());

            return ResponseEntity.ok(Map.of(
                    "message", "Your subscription is set to cancel at the end of your current billing period.",
                    "immediateCancellation", false
            ));
        }

        subscribers.deactivate(email.get());

        return ResponseEntity.ok(Map.of(
                "message", "Your subscription has been cancelled.",
                "immediateCancellation", true
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
        Optional<String> email = subscribers.findEmailByActiveSessionToken(sessionToken);
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
                        .setReturnUrl(appBaseUrl + "/auction-scout/preferences.html")
                        .build();

        com.stripe.model.billingportal.Session portalSession =
                com.stripe.model.billingportal.Session.create(params);
        return ResponseEntity.ok(Map.of("url", portalSession.getUrl()));
    }
}