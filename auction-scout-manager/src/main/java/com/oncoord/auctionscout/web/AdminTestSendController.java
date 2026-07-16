package com.oncoord.auctionscout.web;

import com.oncoord.auctionscout.digest.DigestSendService;
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
 * Goes through DigestSendService like every other send path, so the
 * same cooldown applies here too (confirmed decision: one shared clock
 * across welcome/weekly/test, no bypass for testing).
 */
@RestController
public class AdminTestSendController {

    private final DigestSendService digestSendService;
    private final String adminKey;

    public AdminTestSendController(DigestSendService digestSendService,
                                   @Value("${auctionscout.admin.test-send-key}") String adminKey) {
        this.digestSendService = digestSendService;
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

    private boolean keyMatches(String providedKey) {
        if (adminKey == null || adminKey.isBlank() || providedKey == null) {
            return false;
        }
        byte[] a = adminKey.getBytes(StandardCharsets.UTF_8);
        byte[] b = providedKey.getBytes(StandardCharsets.UTF_8);
        return MessageDigest.isEqual(a, b);
    }
}