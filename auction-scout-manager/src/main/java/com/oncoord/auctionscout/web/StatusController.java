package com.oncoord.auctionscout.web;

import com.oncoord.auctionscout.digest.DigestService;
import com.oncoord.auctionscout.subscriber.SubscriberRepository;
import com.oncoord.auth.common.TokenRecord;
import com.oncoord.auth.common.TokenService;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.time.OffsetDateTime;
import java.util.Arrays;
import java.util.List;
import java.util.Locale;
import java.util.Optional;

/**
 * Thin adapter over DigestService. All the actual generation logic
 * lives in DigestService specifically so the scheduled weekly email job
 * (not built yet) can call DigestService.renderForSubscriber() directly,
 * without going through HTTP at all.
 *
 * getStatusData() (the endpoint status.html actually calls) reads an
 * optional session token to check subscription entitlement (see its
 * own javadoc), but states themselves stay public/unauthenticated --
 * there's still no subscriber identity *required* to call it.
 * getStatus() (plain-HTML view, not currently called by any frontend
 * page) still resolves a required session token -> email, since it's
 * a per-subscriber render and untouched by this change.
 */
@RestController
public class StatusController {

    // Must match DigestService.VIEW_TOKEN_TTL_MILLIS -- the token is
    // minted there and checked here; a mismatch would either reject a
    // link the sender intended to still be valid, or accept one the
    // sender intended to have already expired.
    private static final long VIEW_TOKEN_TTL_MILLIS = 7L * 24 * 60 * 60 * 1000;

    private final SubscriberRepository subscribers;
    private final DigestService digestService;
    private final TokenService tokenService;

    public StatusController(SubscriberRepository subscribers, DigestService digestService, TokenService tokenService) {
        this.subscribers = subscribers;
        this.digestService = digestService;
        this.tokenService = tokenService;
    }

    @GetMapping(value = "/status", produces = MediaType.TEXT_HTML_VALUE)
    public ResponseEntity<String> getStatus(@RequestHeader("X-Session-Token") String sessionToken) {
        Optional<String> email = subscribers.findEmailBySessionToken(sessionToken);
        if (email.isEmpty()) {
            return ResponseEntity.status(401).body("<p>Invalid or expired session</p>");
        }

        // 7-day cutoff, same placeholder reasoning as before: real
        // per-subscriber "last digest sent" tracking is scheduling work,
        // not built yet.
        String html = digestService.renderForSubscriber(email.get(), OffsetDateTime.now().minusDays(7), false).html();
        return ResponseEntity.ok(html);
    }

    /**
     * Structured digest data: the same repository calls and
     * buildChangeGroups rules as getStatus(), just returned as JSON
     * instead of pre-rendered HTML. The status page builds its own DOM
     * and its CSV exports off this one payload, so there's nothing left
     * to independently re-derive that could drift out of sync.
     *
     * X-Session-Token is now optional, not absent: states themselves
     * are still public (an empty/missing states param returns an empty
     * result rather than erroring), but a subscriber's *entitlement* to
     * view more than 1 state at once is checked here, the same
     * hasActiveStripeSubscription() gate used by
     * SubscriberRepository.setStates(). No token, or a token that
     * doesn't resolve to a subscribed subscriber, is treated as free.
     *
     * Resolved gap (previously noted here): weekly digest/saved-alert
     * emails used to link here with a bare, tokenless ?states=, so a
     * subscriber opening their own email without a separate active
     * session got capped to 1 state despite paying for more. Digest
     * links now carry a ?vt= view token (DigestService.statusUrl) that
     * this endpoint resolves via TokenService.peek(token, ttl) below,
     * independent of any session.
     */
    @GetMapping(value = "/status/data", produces = MediaType.APPLICATION_JSON_VALUE)
    public ResponseEntity<?> getStatusData(
            @RequestParam(required = false) String states,
            @RequestParam(required = false) String vt,
            @RequestHeader(value = "X-Session-Token", required = false) String sessionToken) {

        List<String> stateList = (states == null || states.isBlank())
                ? List.of()
                : Arrays.stream(states.split(","))
                .map(String::trim)
                .filter(s -> !s.isEmpty())
                .map(s -> s.toUpperCase(Locale.US))
                .distinct()
                .toList();

        // Two independent ways to identify the caller as a specific
        // subscriber: an existing browser session, or a view token
        // minted into a digest/alert email link (DigestService.statusUrl).
        // The view token is non-consuming (peek, not verifyAndConsume) --
        // reused on every click during its TTL, unlike the one-time
        // login tokens post-login.html consumes.
        Optional<String> subscriberEmail = (sessionToken != null && !sessionToken.isBlank())
                ? subscribers.findEmailBySessionToken(sessionToken)
                : Optional.empty();

        if (subscriberEmail.isEmpty() && vt != null && !vt.isBlank()) {
            subscriberEmail = tokenService.peek(vt, VIEW_TOKEN_TTL_MILLIS)
                    .map(TokenRecord::subject);
        }

        // A view-token link that omits ?states= (e.g. someone forwards
        // just the base URL) falls back to this subscriber's actual
        // saved states rather than showing nothing.
        if (stateList.isEmpty() && subscriberEmail.isPresent()) {
            stateList = subscribers.getStates(subscriberEmail.get()).stream()
                    .map(s -> s.toUpperCase(Locale.US))
                    .distinct()
                    .toList();
        }

        boolean subscribed = subscriberEmail
                .map(subscribers::hasActiveStripeSubscription)
                .orElse(false);

        int maxStates = subscribed ? SubscriberRepository.MAX_STATES_PER_SUBSCRIBER : 1;

        if (stateList.size() > maxStates) {
            return ResponseEntity.status(403).body(java.util.Map.of(
                    "error", subscribed
                            ? "Your plan allows up to " + maxStates + " states at a time."
                            : "Free accounts can view 1 state at a time. " +
                            "Log in with an active subscription to view more.",
                    "maxStates", maxStates
            ));
        }

        DigestService.DigestData data = digestService.renderAsData(stateList, OffsetDateTime.now().minusDays(7));
        return ResponseEntity.ok(data);
    }
}