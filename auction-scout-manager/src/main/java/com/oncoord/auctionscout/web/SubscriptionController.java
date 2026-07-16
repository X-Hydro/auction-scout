package com.oncoord.auctionscout.web;

import com.oncoord.auctionscout.subscriber.SubscriberRepository;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;
import java.util.Optional;

/**
 * Cancel-subscription is deliberately its own controller rather than a
 * third method on PreferencesController: it's a destructive, one-way-
 * feeling action with different UX and (eventually) billing
 * implications once Stripe is wired up, not a preference being saved.
 *
 * No Stripe integration yet -- see SubscriberRepository.deactivate()
 * for what this actually does to the row today (soft-deactivate, not
 * delete). When Stripe is added, this is the seam where a cancellation
 * API call would go, before falling through to the same deactivate().
 */
@RestController
public class SubscriptionController {

    private final SubscriberRepository subscribers;

    public SubscriptionController(SubscriberRepository subscribers) {
        this.subscribers = subscribers;
    }

    @PostMapping("/subscription/cancel")
    public ResponseEntity<?> cancel(@RequestHeader("X-Session-Token") String sessionToken) {
        Optional<String> email = subscribers.findEmailBySessionToken(sessionToken);
        if (email.isEmpty()) {
            return ResponseEntity.status(401).body(Map.of("error", "Invalid or expired session"));
        }

        subscribers.deactivate(email.get());

        return ResponseEntity.ok(Map.of(
                "message", "Your subscription has been cancelled."
        ));
    }
}