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
 * getStatusData() (the endpoint status.html actually calls) is now
 * unauthenticated and state-scoped rather than session-scoped -- states
 * aren't sensitive, so there's no subscriber identity to resolve here
 * anymore. getStatus() (plain-HTML view, not currently called by any
 * frontend page) still resolves session token -> email, since it's a
 * per-subscriber render and untouched by this change.
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
     * No auth -- states are public, so this just reads a plain
     * comma-separated ?states= list off the request instead of
     * resolving a subscriber. An empty/missing states param returns an
     * empty result (findUpcoming/findRecentChanges with an empty IN
     * clause match nothing) rather than erroring.
     */
    @GetMapping(value = "/status/data", produces = MediaType.APPLICATION_JSON_VALUE)
    public ResponseEntity<DigestService.DigestData> getStatusData(
            @RequestParam(required = false) String states) {

        List<String> stateList = (states == null || states.isBlank())
                ? List.of()
                : Arrays.stream(states.split(","))
                .map(String::trim)
                .filter(s -> !s.isEmpty())
                .map(s -> s.toUpperCase(Locale.US))
                .distinct()
                .toList();

        DigestService.DigestData data = digestService.renderAsData(stateList, OffsetDateTime.now().minusDays(7));
        return ResponseEntity.ok(data);
    }
}