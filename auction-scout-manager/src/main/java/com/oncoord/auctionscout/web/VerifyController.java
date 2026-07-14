package com.oncoord.auctionscout.web;

import com.oncoord.auth.common.TokenRecord;
import com.oncoord.auth.common.TokenService;
import com.oncoord.auctionscout.subscriber.SubscriberRepository;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;
import java.util.Optional;

@RestController
public class VerifyController {

    private static final String PURPOSE = "auctionscout";
    private static final long TOKEN_TTL_MILLIS = 30L * 60 * 1000; // 30 minutes

    private final TokenService tokenService;
    private final SubscriberRepository subscribers;

    public VerifyController(TokenService tokenService, SubscriberRepository subscribers) {
        this.tokenService = tokenService;
        this.subscribers = subscribers;
    }

    // Single-step flow: since this table has no purpose column and
    // AuctionScout doesn't split verify/create-user into two requests
    // the way oncoord-manager does, we consume the token in one shot here.
    // If AuctionScout later needs a two-step flow, add a non-consuming
    // peek() call here first, same as the fix applied on oncoord-manager.
    @GetMapping("/verify")
    public ResponseEntity<?> verify(@RequestParam String email, @RequestParam String token) {
        String normalizedEmail = email.trim().toLowerCase();

        Optional<TokenRecord> record = tokenService.verifyAndConsume(
                normalizedEmail, token, TOKEN_TTL_MILLIS
        );

        if (record.isEmpty()) {
            return ResponseEntity.status(400).body(Map.of(
                    "error", "This link is invalid or has expired. Please request a new one."
            ));
        }

        // Doubles as the subscriber's first login: activates the account
        // and issues the storageSession token in one step, matching
        // oncoord-manager's client-side pattern (sessionStorage + a
        // bearer header on future requests) but with its own token,
        // since AuctionScout subscribers aren't oncoord-manager API
        // customers and the two credentials shouldn't be conflated.
        String sessionToken = subscribers.markVerifiedAndIssueSessionToken(normalizedEmail);

        return ResponseEntity.ok(Map.of(
                "message", "Email verified.",
                "email", normalizedEmail,
                "sessionToken", sessionToken
        ));
    }
}