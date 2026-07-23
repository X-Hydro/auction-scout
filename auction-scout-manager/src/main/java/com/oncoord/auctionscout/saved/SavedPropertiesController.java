package com.oncoord.auctionscout.saved;

import com.oncoord.auctionscout.properties.PropertyDigestRepository;
import com.oncoord.auctionscout.subscriber.SubscriberRepository;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.Map;
import java.util.Optional;

/**
 * Uses SubscriberRepository.findEmailBySessionToken() -- the stricter of
 * the two token-resolution methods (vs. findEmailByActiveSessionToken(),
 * which is deliberately looser and reserved for billing endpoints only).
 *
 * Saving (POST) additionally requires hasActiveStripeSubscription() --
 * trial or paid, same access -- matching how email alerts are gated.
 * There's no free tier for this feature. Viewing (GET) and removing
 * (DELETE) are deliberately NOT gated the same way: if a subscription
 * lapses, a subscriber should still be able to see and clean up what
 * they'd already saved, just not add anything new.
 */
@RestController
@RequestMapping("/saved-properties")
public class SavedPropertiesController {

    private final SavedPropertiesRepository savedRepo;
    private final PropertyDigestRepository propertyRepo;
    private final SubscriberRepository subscribers;

    public SavedPropertiesController(SavedPropertiesRepository savedRepo,
                                     PropertyDigestRepository propertyRepo,
                                     SubscriberRepository subscribers) {
        this.savedRepo = savedRepo;
        this.propertyRepo = propertyRepo;
        this.subscribers = subscribers;
    }

    @PostMapping
    public ResponseEntity<?> save(@RequestHeader("X-Session-Token") String sessionToken,
                                  @RequestBody Map<String, Long> body) {
        Optional<String> email = subscribers.findEmailBySessionToken(sessionToken);
        if (email.isEmpty()) {
            return ResponseEntity.status(401).body(Map.of("error", "Invalid or expired session"));
        }
        if (!subscribers.hasActiveStripeSubscription(email.get())) {
            return ResponseEntity.status(403).body(Map.of(
                    "error", "Saving properties requires an active subscription"
            ));
        }

        long propertyId = body.get("propertyId");
        PropertyDigestRepository.PropertyDetails property = propertyRepo.findById(propertyId);
        if (property == null) {
            return ResponseEntity.notFound().build();
        }
        savedRepo.save(email.get(), property);
        return ResponseEntity.status(201).build();
    }

    @DeleteMapping("/{propertyId}")
    public ResponseEntity<?> unsave(@RequestHeader("X-Session-Token") String sessionToken,
                                    @PathVariable long propertyId) {
        Optional<String> email = subscribers.findEmailBySessionToken(sessionToken);
        if (email.isEmpty()) {
            return ResponseEntity.status(401).body(Map.of("error", "Invalid or expired session"));
        }
        savedRepo.delete(email.get(), propertyId);
        return ResponseEntity.noContent().build();
    }

    @GetMapping
    public ResponseEntity<?> list(@RequestHeader("X-Session-Token") String sessionToken) {
        Optional<String> email = subscribers.findEmailBySessionToken(sessionToken);
        if (email.isEmpty()) {
            return ResponseEntity.status(401).body(Map.of("error", "Invalid or expired session"));
        }
        return ResponseEntity.ok(savedRepo.findByEmail(email.get()));
    }
}