package com.oncoord.auctionscout.web;

import com.oncoord.auctionscout.digest.DigestSendService;
import com.oncoord.auctionscout.subscriber.SubscriberRepository;
import com.stripe.exception.StripeException;
import com.stripe.model.Subscription;
import com.stripe.model.SubscriptionItem;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneOffset;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.logging.Level;
import java.util.logging.Logger;

// Only responsible for reading/writing state selections now — status
// display moved to StatusController.
@RestController
public class PreferencesController {

    private static final Logger log = Logger.getLogger(PreferencesController.class.getName());

    // New England states only — matches AuctionScout's actual coverage
    // area. Rejecting anything else here is a light validation layer,
    // not a security boundary (SubscriberRepository already caps at 4
    // states regardless of content).
    private static final Set<String> VALID_STATES = Set.of("ME", "NH", "VT", "MA", "RI", "CT");

    private final SubscriberRepository subscribers;
    private final DigestSendService digestSendService;

    public PreferencesController(SubscriberRepository subscribers, DigestSendService digestSendService) {
        this.subscribers = subscribers;
        this.digestSendService = digestSendService;
    }

    // emailAlertsEnabled defaults to true when omitted, so existing
    // clients that only ever sent {states: [...]} keep working exactly
    // as before instead of accidentally pausing alerts on every save.
    public record SetStatesRequest(List<String> states, Boolean emailAlertsEnabled) {}

    @GetMapping("/preferences")
    public ResponseEntity<?> getPreferences(@RequestHeader("X-Session-Token") String sessionToken) {
        Optional<String> email = subscribers.findEmailBySessionToken(sessionToken);
        if (email.isEmpty()) {
            return ResponseEntity.status(401).body(Map.of("error", "Invalid or expired session"));
        }

        Map<String, Object> response = new HashMap<>();
        response.put("email", email.get());
        response.put("states", subscribers.getStates(email.get()));
        response.put("emailAlertsEnabled", subscribers.getEmailAlertsEnabled(email.get()));
        response.put("hasActiveSubscription", subscribers.hasActiveStripeSubscription(email.get()));

        String status = subscribers.findSubscriptionStatusByEmail(email.get()).orElse(null);
        response.put("subscriptionStatus", status);
        response.put("subscriptionDate", resolveSubscriptionDate(email.get(), status));

        return ResponseEntity.ok(response);
    }

    /**
     * The one date relevant to whichever status the subscriber is
     * currently in -- trial_end for trialing, current_period_end for
     * active, both pulled live from Stripe since neither is tracked
     * locally and both can shift server-side (support-granted trial
     * extensions, renewals, proration) independent of any local write
     * path. subscription_end_date for canceled needs no Stripe call --
     * it's already the local source of truth, set by deactivate().
     *
     * Returns null (not an error) for any other status, or if the
     * Stripe call fails -- a transient Stripe outage shouldn't break
     * the rest of this page. States, the alerts toggle, and Save all
     * still work with a null date; the frontend just omits the
     * status line rather than showing something wrong.
     */
    private String resolveSubscriptionDate(String email, String status) {
        if (status == null) {
            return null;
        }

        if ("canceled".equals(status)) {
            return subscribers.findSubscriptionEndDateByEmail(email)
                    .map(PreferencesController::epochMillisToIsoDate)
                    .orElse(null);
        }

        if (!"trialing".equals(status) && !"active".equals(status)) {
            return null;
        }

        Optional<String> stripeSubscriptionId = subscribers.findStripeSubscriptionIdByEmail(email);
        if (stripeSubscriptionId.isEmpty()) {
            return null;
        }

        try {
            Subscription subscription = Subscription.retrieve(stripeSubscriptionId.get());

            if ("trialing".equals(status)) {
                Long trialEnd = subscription.getTrialEnd();
                return trialEnd == null ? null : epochSecondsToIsoDate(trialEnd);
            }

            // "active" -- current_period_end moved off Subscription onto each
            // SubscriptionItem as of a 2025 Stripe API version (a subscription
            // can now have items on independently staggered billing cycles).
            // AuctionScout only ever creates a subscription with a single
            // item/price, so the first item's period end is the renewal date
            // that matters here.
            List<SubscriptionItem> items = subscription.getItems().getData();
            if (items.isEmpty()) {
                return null;
            }
            Long currentPeriodEnd = items.get(0).getCurrentPeriodEnd();
            return currentPeriodEnd == null ? null : epochSecondsToIsoDate(currentPeriodEnd);
        } catch (StripeException e) {
            log.log(Level.WARNING, "Could not fetch subscription date from Stripe for " + email, e);
            return null;
        }
    }

    private static String epochMillisToIsoDate(long epochMillis) {
        return Instant.ofEpochMilli(epochMillis).atZone(ZoneOffset.UTC).toLocalDate().toString();
    }

    private static String epochSecondsToIsoDate(long epochSeconds) {
        return LocalDate.ofInstant(Instant.ofEpochSecond(epochSeconds), ZoneOffset.UTC).toString();
    }

    @PostMapping("/preferences")
    public ResponseEntity<?> setPreferences(@RequestHeader("X-Session-Token") String sessionToken,
                                            @RequestBody SetStatesRequest req) {
        Optional<String> email = subscribers.findEmailBySessionToken(sessionToken);
        if (email.isEmpty()) {
            return ResponseEntity.status(401).body(Map.of("error", "Invalid or expired session"));
        }

        if (req.states() == null) {
            return ResponseEntity.badRequest().body(Map.of("error", "states is required"));
        }

        List<String> normalized = req.states().stream().map(String::toUpperCase).toList();
        for (String state : normalized) {
            if (!VALID_STATES.contains(state)) {
                return ResponseEntity.badRequest().body(Map.of(
                        "error", "Unsupported state: " + state,
                        "validStates", VALID_STATES
                ));
            }
        }

        try {
            subscribers.setStates(email.get(), normalized);
        } catch (IllegalArgumentException e) {
            return ResponseEntity.badRequest().body(Map.of("error", e.getMessage()));
        }

        boolean emailAlertsEnabled = req.emailAlertsEnabled() == null || req.emailAlertsEnabled();
        subscribers.setEmailAlertsEnabled(email.get(), emailAlertsEnabled);

        // Welcome email: only makes sense if they actually want alerts
        // and have picked at least one state to report on. Safe to call
        // on every save — sendWelcomeIfFirstTime() is itself a no-op
        // after the first time it actually sends (see its javadoc).
        if (emailAlertsEnabled && !normalized.isEmpty()) {
            digestSendService.sendWelcomeIfFirstTime(email.get());
        }

        return ResponseEntity.ok(Map.of(
                "states", normalized,
                "emailAlertsEnabled", emailAlertsEnabled
        ));
    }
}