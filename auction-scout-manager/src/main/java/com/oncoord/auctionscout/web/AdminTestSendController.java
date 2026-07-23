package com.oncoord.auctionscout.web;

import com.oncoord.auctionscout.digest.DigestSendService;
import com.oncoord.auctionscout.digest.SavedPropertyAlertService;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RestController;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Map;

/**
 * Manual one-off digest send for testing — no link to this from
 * anywhere in the app; reachable only by whoever has both the URL and
 * the shared key. "No links to it" alone isn't real protection (the
 * URL can still be found in logs, browser history, this source file,
 * etc.), so this also requires auctionscout.admin.test-send-key,
 * checked in constant time to avoid a timing side-channel on the
 * comparison itself.
 *
 * /admin/test-send (weekly digest) goes through DigestSendService like
 * every other send path, so the same cooldown applies here too
 * (confirmed decision: one shared clock across welcome/weekly/test, no
 * bypass for testing).
 *
 * /admin/test-send-saved-alert is different: SavedPropertyAlertService
 * has no cooldown/anti-spam gate to begin with (see its class javadoc),
 * so there's nothing to preserve by routing through the real send path
 * unmodified -- it deliberately widens the lookback and forces a
 * render instead, since the real path correctly stays silent when
 * there's nothing to report, which is useless for confirming
 * deliverability. See SavedPropertyAlertService.sendTestAlert().
 */
@RestController
public class AdminTestSendController {

    private final DigestSendService digestSendService;
    private final SavedPropertyAlertService savedPropertyAlertService;
    private final String adminKey;

    public AdminTestSendController(DigestSendService digestSendService,
                                   SavedPropertyAlertService savedPropertyAlertService,
                                   @Value("${auctionscout.admin.test-send-key}") String adminKey) {
        this.digestSendService = digestSendService;
        this.savedPropertyAlertService = savedPropertyAlertService;
        this.adminKey = adminKey;
    }

    public record TestSendRequest(String email) {}

    @PostMapping("/admin/test-send")
    public ResponseEntity<?> testSend(@RequestHeader(value = "X-Admin-Key", required = false) String providedKey,
                                      @RequestBody TestSendRequest req) {
        if (!keyMatches(providedKey)) {
            return ResponseEntity.status(401).body(Map.of("error", "Unauthorized"));
        }
        if (req.email() == null || req.email().isBlank()) {
            return ResponseEntity.badRequest().body(Map.of("error", "email is required"));
        }

        DigestSendService.SendResult result =
                digestSendService.sendTest(req.email().trim().toLowerCase());

        return ResponseEntity.ok(Map.of("result", result.name()));
    }

    @PostMapping("/admin/test-send-saved-alert")
    public ResponseEntity<?> testSendSavedAlert(@RequestHeader(value = "X-Admin-Key", required = false) String providedKey,
                                                @RequestBody TestSendRequest req) {
        if (!keyMatches(providedKey)) {
            return ResponseEntity.status(401).body(Map.of("error", "Unauthorized"));
        }
        if (req.email() == null || req.email().isBlank()) {
            return ResponseEntity.badRequest().body(Map.of("error", "email is required"));
        }

        SavedPropertyAlertService.TestResult result =
                savedPropertyAlertService.sendTestAlert(req.email().trim().toLowerCase());

        return ResponseEntity.ok(Map.of("result", result.name()));
    }

    /**
     * The REAL saved-property alert run -- emails every actively-
     * subscribed, alerts-enabled subscriber who has a saved property
     * with a recent date change or removal. Not a test: this is the
     * production send, manually triggered (see class javadoc) instead
     * of running on a fixed cron. No request body -- there's no single
     * target address, it runs against everyone eligible. Returns
     * sentCount so the admin page can show something more useful than
     * "it ran" -- most eligible subscribers on any given run will have
     * nothing to report and are silently skipped.
     */
    @PostMapping("/admin/run-saved-property-alerts")
    public ResponseEntity<?> runSavedPropertyAlerts(@RequestHeader(value = "X-Admin-Key", required = false) String providedKey) {
        if (!keyMatches(providedKey)) {
            return ResponseEntity.status(401).body(Map.of("error", "Unauthorized"));
        }

        int sentCount = savedPropertyAlertService.sendToAllActiveSubscribers();

        return ResponseEntity.ok(Map.of("result", "DONE", "sentCount", sentCount));
    }

    private boolean keyMatches(String providedKey) {
        if (adminKey == null || adminKey.isBlank() || providedKey == null) {
            return false;
        }
        byte[] a = adminKey.getBytes(StandardCharsets.UTF_8);
        byte[] b = providedKey.getBytes(StandardCharsets.UTF_8);
        return MessageDigest.isEqual(a, b);
    }
}