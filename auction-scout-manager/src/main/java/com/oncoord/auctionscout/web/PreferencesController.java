package com.oncoord.auctionscout.web;

import com.oncoord.auctionscout.subscriber.SubscriberRepository;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

@RestController
public class PreferencesController {

    // New England states only — matches AuctionScout's actual coverage
    // area. Rejecting anything else here is a light validation layer,
    // not a security boundary (SubscriberRepository already caps at 4
    // states regardless of content).
    private static final Set<String> VALID_STATES = Set.of("ME", "NH", "VT", "MA", "RI", "CT");

    private final SubscriberRepository subscribers;

    public PreferencesController(SubscriberRepository subscribers) {
        this.subscribers = subscribers;
    }

    public record SetStatesRequest(List<String> states) {}

    @GetMapping("/preferences")
    public ResponseEntity<?> getPreferences(@RequestHeader("X-Session-Token") String sessionToken) {
        Optional<String> email = subscribers.findEmailBySessionToken(sessionToken);
        if (email.isEmpty()) {
            return ResponseEntity.status(401).body(Map.of("error", "Invalid or expired session"));
        }

        return ResponseEntity.ok(Map.of(
                "email", email.get(),
                "states", subscribers.getStates(email.get())
        ));
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

        return ResponseEntity.ok(Map.of("states", normalized));
    }
}