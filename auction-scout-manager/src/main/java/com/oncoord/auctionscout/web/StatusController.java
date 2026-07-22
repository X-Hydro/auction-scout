package com.oncoord.auctionscout.web;

import com.oncoord.auctionscout.digest.DigestService;
import com.oncoord.auctionscout.subscriber.SubscriberRepository;
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

    private final SubscriberRepository subscribers;
    private final DigestService digestService;

    public StatusController(SubscriberRepository subscribers, DigestService digestService) {
        this.subscribers = subscribers;
        this.digestService = digestService;
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
        String html = digestService.renderForSubscriber(email.get(), OffsetDateTime.now().minusDays(7), false);
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
     * Known gap: weekly digest emails link here with the subscriber's
     * full state list embedded directly in ?states=, and that link
     * carries no token -- a subscriber opening their own digest email
     * on a browser where they aren't separately logged in will get
     * capped to 1 state here despite paying for more. Revisit with a
     * signed, subscriber-scoped link (mirroring the TokenService magic
     * link pattern) if that turns out to be a real problem in practice.
     */
    @GetMapping(value = "/status/data", produces = MediaType.APPLICATION_JSON_VALUE)
    public ResponseEntity<?> getStatusData(
            @RequestParam(required = false) String states,
            @RequestHeader(value = "X-Session-Token", required = false) String sessionToken) {

        List<String> stateList = (states == null || states.isBlank())
                ? List.of()
                : Arrays.stream(states.split(","))
                .map(String::trim)
                .filter(s -> !s.isEmpty())
                .map(s -> s.toUpperCase(Locale.US))
                .distinct()
                .toList();

        boolean subscribed = sessionToken != null && !sessionToken.isBlank()
                && subscribers.findEmailBySessionToken(sessionToken)
                .map(subscribers::hasActiveStripeSubscription)
                .orElse(false);

        if (!subscribed && stateList.size() > 1) {
            return ResponseEntity.status(403).body(java.util.Map.of(
                    "error", "Free accounts can view 1 state at a time. " +
                            "Log in with an active subscription to view more.",
                    "maxStates", 1
            ));
        }

        DigestService.DigestData data = digestService.renderAsData(stateList, OffsetDateTime.now().minusDays(7));
        return ResponseEntity.ok(data);
    }
}