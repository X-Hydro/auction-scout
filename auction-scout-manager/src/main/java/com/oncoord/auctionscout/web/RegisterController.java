package com.oncoord.auctionscout.web;

import com.oncoord.auth.common.RecaptchaClient;
import com.oncoord.auth.common.TokenService;
import com.oncoord.auctionscout.subscriber.SubscriberRepository;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

@RestController
public class RegisterController {

    // Single implicit purpose (per decision: this table has no purpose
    // column, so every token issued from AuctionScoutTokenStore is scoped
    // by this constant rather than by a persisted column).

    private final RecaptchaClient recaptchaClient;
    private final TokenService tokenService;
    private final SubscriberRepository subscribers;
    // TODO: swap in OneTimeLinkMailer from oncoord-auth-common once wired
    // — for now this just logs the raw link so you can test the loop.

    public RegisterController(RecaptchaClient recaptchaClient,
                              TokenService tokenService,
                              SubscriberRepository subscribers) {
        this.recaptchaClient = recaptchaClient;
        this.tokenService = tokenService;
        this.subscribers = subscribers;
    }

    // Field named "captcha" (not "recaptchaToken") to match the request
    // shape oncoord-manager's login.html already sends to /send-login-link.
    public record RegisterRequest(String email, String captcha) {}

    @PostMapping("/register")
    public ResponseEntity<?> register(@RequestBody RegisterRequest req) {
        if (req.email() == null || req.email().isBlank()) {
            return ResponseEntity.badRequest().body(Map.of("error", "email is required"));
        }
        if (req.captcha() == null || req.captcha().isBlank()) {
            return ResponseEntity.badRequest().body(Map.of("error", "CAPTCHA is required"));
        }

        boolean recaptchaOk = recaptchaClient.verify(req.captcha());
        if (!recaptchaOk) {
            return ResponseEntity.status(400).body(Map.of("error", "CAPTCHA verification failed"));
        }

        String email = req.email().trim().toLowerCase();

        // Always behave the same whether the email already exists or not —
        // avoids using this endpoint to enumerate subscribers.
        if (!subscribers.existsByEmail(email)) {
            subscribers.createUnverified(email);
        }

        String rawToken = tokenService.issue(email);

        // TODO: replace with OneTimeLinkMailer.sendRegistrationLink(email, rawToken)
        System.out.println("🔗 [DEV ONLY] Magic link for " + email
                + ": https://www.oncoord.com/auction-scout/post-login.html#email="
                + java.net.URLEncoder.encode(email, java.nio.charset.StandardCharsets.UTF_8)
                + "&token=" + rawToken);

        return ResponseEntity.ok(Map.of(
                "message", "If that email is valid, a confirmation link has been sent."
        ));
    }
}