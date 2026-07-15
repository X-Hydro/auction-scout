package com.oncoord.auctionscout.web;

import com.oncoord.auctionscout.subscriber.SubscriberRepository;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RestController;

/**
 * Explicit, server-side session revocation. sessionStorage already
 * clears the token on tab close for free, and status.html/
 * preferences.html slide the client-side expiry forward on every
 * successful authenticated action so real work sessions are never
 * interrupted -- but neither of those does anything about the token
 * itself, which SubscriberRepository documents as having no timed
 * expiry server-side. This endpoint is the one path that actually
 * invalidates the credential, for the case a user explicitly wants to
 * end their session (shared computer, just done for the day, etc.)
 * rather than relying on the tab eventually closing.
 */
@RestController
public class LogoutController {

    private final SubscriberRepository subscribers;

    public LogoutController(SubscriberRepository subscribers) {
        this.subscribers = subscribers;
    }

    @PostMapping("/logout")
    public ResponseEntity<Void> logout(@RequestHeader("X-Session-Token") String sessionToken) {
        // Always 204, whether or not the token matched a row -- an
        // error response here would let a caller distinguish "valid
        // token, now revoked" from "already invalid," which is exactly
        // the kind of oracle a session endpoint shouldn't offer.
        subscribers.invalidateSessionToken(sessionToken);
        return ResponseEntity.noContent().build();
    }
}