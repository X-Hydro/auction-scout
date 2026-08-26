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
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;

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
public class AdminEmailController {

    private final DigestSendService digestSendService;
    private final SavedPropertyAlertService savedPropertyAlertService;
    private final String adminKey;


    private final AtomicBoolean savedPropertyAlertRunInProgress = new AtomicBoolean(false);
    private final AtomicBoolean digestRunInProgress = new AtomicBoolean(false);

    public AdminEmailController(DigestSendService digestSendService,
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

    public record RunSavedPropertyAlertsRequest(Boolean dryRun) {}

    /**
     * The REAL saved-property alert run -- emails every actively-
     * subscribed, alerts-enabled subscriber who has a saved property
     * with a recent date change or removal. Not a test: this is the
     * production send, manually triggered (see class javadoc) instead
     * of running on a fixed cron. Returns sentCount so the admin page
     * can show something more useful than "it ran" -- most eligible
     * subscribers on any given run will have nothing to report and are
     * silently skipped.
     *
     * Request body is optional and only carries dryRun -- there's no
     * single target address either way, it always runs against every
     * eligible subscriber. When dryRun is true, this renders exactly
     * what each subscriber would receive (same subscriber set, same
     * cutoff logic as the real send) but never sends mail and never
     * advances anyone's real "last sent" cutoff -- see
     * SavedPropertyAlertService.previewAllActiveSubscribers(). Shares
     * the same in-progress guard as the real run since both walk the
     * full subscriber set and render every eligible email; no reason to
     * let a preview and a real run stomp on each other.
     */
    @PostMapping("/admin/run-saved-property-alerts")
    public ResponseEntity<?> runSavedPropertyAlerts(@RequestHeader(value = "X-Admin-Key", required = false) String providedKey,
                                                    @RequestBody(required = false) RunSavedPropertyAlertsRequest req) {
        if (!keyMatches(providedKey)) {
            return ResponseEntity.status(401).body(Map.of("error", "Unauthorized"));
        }

        boolean dryRun = req != null && Boolean.TRUE.equals(req.dryRun());

        if (!savedPropertyAlertRunInProgress.compareAndSet(false, true)) {
            return ResponseEntity.status(409).body(Map.of(
                    "error", "A saved-property alert run is already in progress. Wait for it to finish before starting another."
            ));
        }

        try {
            if (dryRun) {
                List<SavedPropertyAlertService.PreviewResult> preview =
                        savedPropertyAlertService.previewAllActiveSubscribers();
                List<Map<String, String>> results = preview.stream()
                        .map(r -> Map.of("email", r.email(), "subject", r.subject(), "html", r.html()))
                        .toList();
                return ResponseEntity.ok(Map.of("dryRun", true, "results", results));
            }

            int sentCount = savedPropertyAlertService.sendToAllActiveSubscribers();
            return ResponseEntity.ok(Map.of("result", "DONE", "sentCount", sentCount));
        } finally {
            // Always released, success or exception -- otherwise one
            // failed run would permanently wedge every future run behind
            // a flag nothing will ever clear.
            savedPropertyAlertRunInProgress.set(false);
        }
    }


    @PostMapping("/admin/run-digest")
    public ResponseEntity<?> runDigest(@RequestHeader(value = "X-Admin-Key", required = false) String providedKey) {
        if (!keyMatches(providedKey)) {
            return ResponseEntity.status(401).body(Map.of("error", "Unauthorized"));
        }

        if (!digestRunInProgress.compareAndSet(false, true)) {
            return ResponseEntity.status(409).body(Map.of(
                    "error", "A digest run is already in progress. Wait for it to finish before starting another."
            ));
        }

        try {
            int sentCount = digestSendService.sendWeeklyToAllActiveSubscribers();
            return ResponseEntity.ok(Map.of("result", "DONE", "sentCount", sentCount));
        } finally {
            digestRunInProgress.set(false);
        }
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