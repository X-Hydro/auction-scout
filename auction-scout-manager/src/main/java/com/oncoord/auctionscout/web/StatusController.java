package com.oncoord.auctionscout.web;

import com.oncoord.auctionscout.digest.DigestService;
import com.oncoord.auctionscout.subscriber.SubscriberRepository;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RestController;

import java.time.OffsetDateTime;
import java.util.Optional;

/**
 * Thin adapter: resolve session token -> email, hand off to
 * DigestService. All the actual generation logic lives in DigestService
 * specifically so the scheduled weekly email job (not built yet) can
 * call DigestService.renderForSubscriber() directly, without going
 * through HTTP or a session token at all.
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
     */
    @GetMapping(value = "/status/data", produces = MediaType.APPLICATION_JSON_VALUE)
    public ResponseEntity<DigestService.DigestData> getStatusData(@RequestHeader("X-Session-Token") String sessionToken) {
        Optional<String> email = subscribers.findEmailBySessionToken(sessionToken);
        if (email.isEmpty()) {
            return ResponseEntity.status(401).build();
        }

        DigestService.DigestData data = digestService.renderForSubscriberAsData(email.get(), OffsetDateTime.now().minusDays(7));
        return ResponseEntity.ok(data);
    }
}