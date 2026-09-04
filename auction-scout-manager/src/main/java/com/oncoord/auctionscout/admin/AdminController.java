package com.oncoord.auctionscout.admin;

import com.oncoord.auctionscout.subscriber.SubscriberRepository;
import com.oncoord.auctionscout.subscriber.SubscriberRepository.AdminSubscriberRow;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.List;

/**
 * Read-only admin dashboard for AuctionScout subscribers.
 *
 * Auth: a single shared-secret token passed as a query param, same
 * pattern as the OnCoord admin page (?token=...). Trade-off vs a
 * header-based scheme: this token will end up in server access logs,
 * browser history, and any Referer header sent from this page --
 * acceptable here since the data behind it is just subscriber
 * emails/status, not credentials or payment info. Set admin.secret.token
 * in application.properties (sourced from .env, same as your other
 * secrets) -- never commit a real value.
 *
 * Tier classification below mirrors SubscriberRepository.hasActiveAccess()
 * exactly (same TRIAL_WINDOW_MILLIS = 30 days), computed here instead of
 * called per-row for the same N+1 reason as findActiveWithAlertsEnabled().
 * If hasActiveAccess()'s rules ever change, update TRIAL_WINDOW_MILLIS and
 * isWithinTrial() below to match -- see that method's javadoc for why
 * these two definitions need to stay in sync.
 */
@RestController
public class AdminController {

    private static final long TRIAL_WINDOW_MILLIS = 30L * 24 * 60 * 60 * 1000;
    private static final DateTimeFormatter DATE_FMT =
            DateTimeFormatter.ofPattern("yyyy-MM-dd").withZone(ZoneOffset.UTC);

    private final SubscriberRepository subscriberRepository;
    private final String adminSecretToken;

    public AdminController(
            SubscriberRepository subscriberRepository,
            @Value("${admin.secret.token}") String adminSecretToken
    ) {
        this.subscriberRepository = subscriberRepository;
        this.adminSecretToken = adminSecretToken;
    }

    private enum Tier { PAID, TRIAL, EXPIRED, CANCELLED }

    @GetMapping(value = "/admin/users", produces = MediaType.TEXT_HTML_VALUE)
    public ResponseEntity<String> users(@RequestParam(value = "token", required = false) String token) {
        if (token == null || !constantTimeEquals(token, adminSecretToken)) {
            // 404 rather than 401/403 -- doesn't confirm to a prober that
            // /auction-scout/admin/users exists at all, just that whatever
            // they hit didn't resolve to anything.
            return ResponseEntity.status(HttpStatus.NOT_FOUND).build();
        }

        List<AdminSubscriberRow> subscribers = subscriberRepository.findAllForAdmin();

        long total = subscribers.size();
        long paid = subscribers.stream().filter(s -> tierOf(s) == Tier.PAID).count();
        long trial = subscribers.stream().filter(s -> tierOf(s) == Tier.TRIAL).count();
        long expired = subscribers.stream().filter(s -> tierOf(s) == Tier.EXPIRED).count();
        long cancelled = subscribers.stream().filter(s -> tierOf(s) == Tier.CANCELLED).count();

        StringBuilder rows = new StringBuilder();
        for (AdminSubscriberRow s : subscribers) {
            Tier tier = tierOf(s);
            rows.append("<tr class=\"").append(rowClass(tier)).append("\">")
                    .append(td(s.email()))
                    .append(td(badge(tier)))
                    .append(td(s.stripeSubscriptionStatus()))
                    .append(td(formatMillis(s.subscriptionStartDate())))
                    .append(td(formatMillis(s.subscriptionEndDate())))
                    .append(td(formatMillis(s.createdAt())))
                    .append("</tr>\n");
        }

        String html = """
            <!DOCTYPE html>
            <html>
            <head>
              <meta charset="UTF-8">
              <title>AuctionScout Admin</title>
              <meta http-equiv="refresh" content="600">
              <style>
                * { box-sizing: border-box; }
                body  { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                        margin: 0; padding: 24px; background: #f4f6fb; color: #333; }
                h1    { font-size: 22px; margin-bottom: 4px; }
                .meta { font-size: 13px; color: #888; margin-bottom: 24px; }
                .cards { display: flex; gap: 16px; margin-bottom: 28px; flex-wrap: wrap; }
                .card  { background: white; border-radius: 8px; padding: 16px 24px;
                         box-shadow: 0 1px 4px rgba(0,0,0,0.08); min-width: 110px; }
                .label { font-size: 12px; color: #888; text-transform: uppercase;
                         letter-spacing: .5px; margin-bottom: 6px; }
                .value { font-size: 28px; font-weight: 700; }
                .card.paid     .value { color: #27ae60; }
                .card.trial    .value { color: #2980b9; }
                .card.expired  .value { color: #e67e22; }
                .card.cancelled .value { color: #c0392b; }
                table { width: 100%%; border-collapse: collapse; background: white;
                        border-radius: 8px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }
                th    { background: #f0f2f8; padding: 10px 14px; text-align: left;
                        font-size: 12px; text-transform: uppercase; letter-spacing: .5px; color: #555; }
                td    { padding: 10px 14px; font-size: 14px; border-top: 1px solid #f0f0f0; }
                tr.paid td:first-child      { border-left: 3px solid #27ae60; }
                tr.trial td:first-child     { border-left: 3px solid #2980b9; }
                tr.expired td:first-child   { border-left: 3px solid #e67e22; }
                tr.cancelled td             { color: #aaa; }
                tr.cancelled td:first-child { border-left: 3px solid #e0e0e0; }
                .badge { display: inline-block; padding: 2px 8px; border-radius: 10px;
                         font-size: 12px; font-weight: 600; text-transform: uppercase; }
                .badge-paid      { background: #eaf6ee; color: #27ae60; }
                .badge-trial     { background: #eaf3fb; color: #2980b9; }
                .badge-expired   { background: #fdf1e6; color: #e67e22; }
                .badge-cancelled { background: #ecf0f1; color: #7f8c8d; }
                .footer { margin-top: 16px; font-size: 12px; color: #aaa; text-align: right; }
              </style>
            </head>
            <body>
              <h1>AuctionScout Admin</h1>
              <div class="meta">Generated %s UTC</div>
              <div class="cards">
                <div class="card"><div class="label">Total</div><div class="value">%d</div></div>
                <div class="card paid"><div class="label">Paid</div><div class="value">%d</div></div>
                <div class="card trial"><div class="label">Trial</div><div class="value">%d</div></div>
                <div class="card expired"><div class="label">Expired</div><div class="value">%d</div></div>
                <div class="card cancelled"><div class="label">Cancelled</div><div class="value">%d</div></div>
              </div>
              <table>
                <thead><tr>
                  <th>Email</th><th>Tier</th><th>Stripe Status</th>
                  <th>Trial/Sub Start</th><th>Ended</th><th>Created</th>
                </tr></thead>
                <tbody>
                %s
                </tbody>
              </table>
              <div class="footer">Token-protected &middot; not linked in nav</div>
            </body>
            </html>
            """.formatted(
                Instant.now().toString(), total, paid, trial, expired, cancelled, rows.toString()
        );

        return ResponseEntity.ok(html);
    }

    private static boolean constantTimeEquals(String a, String b) {
        return MessageDigest.isEqual(
                a.getBytes(StandardCharsets.UTF_8),
                b.getBytes(StandardCharsets.UTF_8)
        );
    }

    /**
     * Mirrors SubscriberRepository.hasActiveAccess() plus the
     * is_active split that method doesn't need to make: hasActiveAccess()
     * only answers "does this email have access right now," but the
     * admin view also needs to distinguish a lapsed-but-still-registered
     * trial (is_active = 1, deactivate() never called) from an explicit
     * cancellation (is_active = 0, set by deactivate()).
     */
    private Tier tierOf(AdminSubscriberRow s) {
        boolean stripeActive = "active".equals(s.stripeSubscriptionStatus())
                || "trialing".equals(s.stripeSubscriptionStatus());
        if (!s.isActive()) {
            return Tier.CANCELLED;
        }
        if (stripeActive) {
            return Tier.PAID;
        }
        return isWithinTrial(s.subscriptionStartDate()) ? Tier.TRIAL : Tier.EXPIRED;
    }

    private boolean isWithinTrial(Long subscriptionStartDate) {
        if (subscriptionStartDate == null) {
            return false;
        }
        long age = System.currentTimeMillis() - subscriptionStartDate;
        return age >= 0 && age <= TRIAL_WINDOW_MILLIS;
    }

    private String rowClass(Tier tier) {
        return tier.name().toLowerCase();
    }

    private String badge(Tier tier) {
        return "<span class=\"badge badge-" + rowClass(tier) + "\">" + rowClass(tier) + "</span>";
    }

    private String formatMillis(Long millis) {
        return millis == null ? "—" : DATE_FMT.format(Instant.ofEpochMilli(millis));
    }

    private String td(Object val) {
        return "<td>" + (val == null || val.toString().isBlank() ? "—" : val) + "</td>";
    }
}