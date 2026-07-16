package com.oncoord.auctionscout.digest;

import com.oncoord.auctionscout.properties.PropertyDigestRepository;
import com.oncoord.auctionscout.properties.PropertyDigestRepository.ChangedListing;
import com.oncoord.auctionscout.properties.PropertyDigestRepository.UpcomingListing;
import com.oncoord.auctionscout.subscriber.SubscriberRepository;
import com.oncoord.auth.common.TokenService;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.time.LocalDateTime;
import java.time.OffsetDateTime;
import java.time.format.DateTimeFormatter;
import java.util.List;
import java.util.Locale;

/**
 * Renders the weekly digest HTML — same structure/CSS as
 * email_preview.html — from live data in auctionscout.db. Deliberately
 * "dumb": renders whatever event_type/old_value/new_value strings are
 * actually in the database verbatim, no attempt to interpret or
 * categorize them into a fixed tag vocabulary. See
 * PropertyDigestRepository's javadoc for why.
 *
 * renderForSubscriber() is the entry point meant for reuse — both the
 * web-facing status page (via a Controller resolving a session token to
 * an email) and the scheduled weekly email job call this same method
 * rather than duplicating the "look up states, then render" logic in
 * two places. The `truncate` flag is how those two callers differ:
 * StatusController passes false (show everything — it's the page the
 * truncated email points people back to), DigestSendService passes true
 * (cap each section so the email stays short).
 */
@Service
public class DigestService {

    private static final DateTimeFormatter DAY_HEADER = DateTimeFormatter.ofPattern("EEEE, MMMM d", Locale.US);
    private static final DateTimeFormatter LISTING_META = DateTimeFormatter.ofPattern("MM/dd 'at' h:mm a", Locale.US);

    // "New" listing noise filter (see renderChanges): a brand-new listing
    // whose auction is more than FAR_OUT_DAYS away, and that hasn't
    // accumulated at least MIN_HISTORY_DAYS of confirmed observation
    // (last_seen_at - first_seen_at), is suppressed from the digest
    // rather than announced as "New". Tune both here, in one place.
    private static final int FAR_OUT_DAYS = 30;
    private static final int MIN_HISTORY_DAYS = 7;

    // Email truncation caps — only applied when truncate=true. Chosen
    // to keep the email short enough to skim; the web status page
    // (truncate=false) always shows everything, since it's exactly
    // where the "+N more" / "View all" links in the email point.
    private static final int MAX_LISTINGS_PER_DAY = 5;
    private static final int MAX_CHANGES_PER_BUCKET = 5;

    // Status-change rows only get a "View listing/map" link if the
    // auction is coming up soon -- a change on something 3 months out
    // is informational, not something to act on immediately, so the
    // extra link is noise there. Tune here if the cutoff needs adjusting.
    private static final int NEW_LISTING_LINK_WINDOW_DAYS  = 30;

    // render() emits these literal placeholders instead of a real URL
    // for the two "View all ..." links, rather than resolving them
    // itself — it has no subscriber email to build a personalized
    // auto-login link with, and no reason to (see renderForSubscriber(),
    // the only place that knows both the email and whether this is
    // going into an email vs the web page). Two distinct placeholders,
    // not one shared: each digest link needs its own single-use token,
    // since a magic-link token only verifies once — sharing one between
    // both links would leave whichever gets clicked second showing
    // "invalid or expired". A caller that invokes render() directly
    // (e.g. a unit test with a bare state list) will see these literal
    // strings in the output instead of a link if truncation ever
    // actually kicks in for it — expected, not a bug, since only
    // renderForSubscriber() fills them in.
    private static final String UPCOMING_LINK_PLACEHOLDER = "{{STATUS_LINK_UPCOMING}}";
    private static final String CHANGES_LINK_PLACEHOLDER = "{{STATUS_LINK_CHANGES}}";

    private final PropertyDigestRepository repository;
    private final SubscriberRepository subscribers;
    private final TokenService tokenService;
    private final String appBaseUrl;

    public DigestService(PropertyDigestRepository repository,
                         SubscriberRepository subscribers,
                         TokenService tokenService,
                         @Value("${auctionscout.app.base-url}") String appBaseUrl) {
        this.repository = repository;
        this.subscribers = subscribers;
        this.tokenService = tokenService;
        this.appBaseUrl = appBaseUrl;
    }

    /**
     * Looks up the subscriber's saved states, renders their digest, then
     * fills in the "View all ..." links render() left as placeholders.
     * For truncate=true (email), each link gets its own fresh, single-
     * use, 30-minute auto-login token pointing at post-login.html#...
     * &redirect=/auction-scout/status.html — clicking it verifies and
     * lands the subscriber straight on their status page, no CAPTCHA/
     * re-registration hoop, matching the existing registration-link
     * mechanism exactly (same TokenService, same /verify endpoint).
     * For truncate=false (the web status page itself), the placeholders
     * never actually appear in the rendered output in the first place
     * (nothing is ever truncated there), so this only replaces them as
     * a harmless no-op safety net, not a real path.
     */
    public String renderForSubscriber(String email, OffsetDateTime changesSince, boolean truncate) {
        List<String> states = subscribers.getStates(email);
        String html = render(states, changesSince, truncate);

        if (!truncate) {
            String plainUrl = appBaseUrl + "/auction-scout/status.html";
            return html.replace(UPCOMING_LINK_PLACEHOLDER, plainUrl)
                    .replace(CHANGES_LINK_PLACEHOLDER, plainUrl);
        }

        return html.replace(UPCOMING_LINK_PLACEHOLDER, buildAutoLoginLink(email))
                .replace(CHANGES_LINK_PLACEHOLDER, buildAutoLoginLink(email));
    }

    /**
     * Issues a fresh magic-link token (same TokenService, same
     * login_tokens table as registration) and points it at
     * post-login.html with a redirect back to status.html. The 30-
     * minute expiry isn't enforced here — issue() itself has no
     * concept of a TTL — it's enforced entirely by VerifyController
     * when the token is consumed, same as every other magic link.
     */
    private String buildAutoLoginLink(String email) {
        String rawToken = tokenService.issue(email);
        return appBaseUrl + "/auction-scout/post-login.html#email="
                + URLEncoder.encode(email, StandardCharsets.UTF_8)
                + "&token=" + URLEncoder.encode(rawToken, StandardCharsets.UTF_8)
                + "&redirect=" + URLEncoder.encode("/auction-scout/status.html", StandardCharsets.UTF_8);
    }

    /**
     * @param changesSince cutoff for the "what changed" section — until
     *                      per-subscriber "last digest sent" tracking
     *                      exists (scheduling work, not built yet),
     *                      callers should pass something reasonable
     *                      like "7 days ago" rather than a real
     *                      per-subscriber value.
     * @param truncate      true for email (cap each section at 5, with
     *                      "+N more" / "View all" links back to the web
     *                      status page); false to show everything (the
     *                      web status page itself).
     */
    public String render(List<String> states, OffsetDateTime changesSince, boolean truncate) {
        LocalDateTime now = LocalDateTime.now();
        List<UpcomingListing> upcoming = repository.findUpcoming(states, now, now.plusDays(7));
        List<ChangedListing> changes = repository.findRecentChanges(states, changesSince);

        return """
                <html><head><base target="_top"><style>
                    body { font-family: -apple-system, Helvetica, Arial, sans-serif; color: #1a1a1a; margin:0; padding:0; background:#f4f4f4; }
                    .container { max-width: 640px; margin: 0 auto; background:#ffffff; }
                    .header { background:#1a3a5c; color:#ffffff; padding:24px 32px; }
                    .header h1 { margin:0; font-size:20px; }
                    .header p { margin:4px 0 0; font-size:13px; opacity:0.85; }
                    .section { padding:24px 32px; border-bottom:1px solid #eaeaea; }
                    .section h2 { font-size:16px; margin:0 0 16px; color:#1a3a5c; }
                    .day-header { font-size:13px; font-weight:600; color:#666; margin:16px 0 8px; text-transform:uppercase; letter-spacing:0.03em; }
                    .listing { padding:10px 0; border-bottom:1px solid #f0f0f0; }
                    .listing:last-child { border-bottom:none; }
                    .listing .addr { font-weight:600; font-size:14px; }
                    .listing .meta { font-size:13px; color:#666; margin-top:2px; }
                    .listing a { color:#1a5c9c; text-decoration:none; font-size:13px; }
                    table.status-table { width:100%%; border-collapse:collapse; font-size:13px; }
                    table.status-table td { padding:8px 4px; border-bottom:1px solid #f0f0f0; }
                    table.status-table a { color:#1a5c9c; text-decoration:none; }
                    .tag { display:inline-block; padding:2px 8px; border-radius:3px; font-size:11px; font-weight:600; background:#eef0f4; color:#3a4556; }
                    .empty { color:#999; font-size:13px; font-style:italic; }
                    .footer { padding:20px 32px; font-size:11px; color:#999; }
                </style></head><body><div class='container'>
                <div class='header'><h1>AuctionScout Auction Watch</h1><p>Weekly update — %s</p></div>
                <div class='section'><h2>Auctions in the Next 7 Days</h2>
                %s
                </div>
                <div class='section'><h2>Status Changes</h2>
                %s
                </div>
                <div class='footer'>You're receiving this because you subscribed to AuctionScout Auction Watch. <a href='#'>Manage preferences</a> · <a href='#'>Unsubscribe</a></div>
                </div></body></html>
                """.formatted(
                now.format(DateTimeFormatter.ofPattern("MMMM d, yyyy", Locale.US)),
                renderUpcoming(upcoming, truncate, UPCOMING_LINK_PLACEHOLDER),
                renderChanges(changes, truncate, CHANGES_LINK_PLACEHOLDER)
        );
    }

    private String renderUpcoming(List<UpcomingListing> upcoming, boolean truncate, String viewAllLinkHref) {
        // Group by day first so the per-day cap applies within each day,
        // not across the whole 7-day window.
        java.util.Map<String, List<UpcomingListing>> byDay = new java.util.LinkedHashMap<>();
        for (UpcomingListing listing : upcoming) {
            // Unparseable auction_datetime -> no safe day/time to group
            // under, so it's skipped from this view. If that's hiding
            // real listings, it's a sign the source data needs cleanup
            // at the pipeline, per the "dumb frontend" decision — not
            // something to special-case here.
            if (listing.auctionDateTime() == null) {
                continue;
            }
            String day = listing.auctionDateTime().format(DAY_HEADER);
            byDay.computeIfAbsent(day, k -> new java.util.ArrayList<>()).add(listing);
        }

        if (byDay.isEmpty()) {
            return "<p class='empty'>No auctions in the next 7 days for your selected states.</p>";
        }

        StringBuilder html = new StringBuilder();
        for (var entry : byDay.entrySet()) {
            List<UpcomingListing> dayListings = entry.getValue();
            html.append("<div class='day-header'>").append(entry.getKey()).append("</div>\n");

            int shown = truncate ? Math.min(MAX_LISTINGS_PER_DAY, dayListings.size()) : dayListings.size();
            for (int i = 0; i < shown; i++) {
                html.append(renderOneUpcoming(dayListings.get(i)));
            }

            int remaining = dayListings.size() - shown;
            if (remaining > 0) {
                // Linked right where the count is shown, rather than
                // one summary link at the bottom of the whole section --
                // each day's overflow is its own reason to click through.
                html.append("<div class='listing'><span class='empty'>+%d more auction%s this day — <a href='%s'>view all →</a></span></div>\n"
                        .formatted(remaining, remaining == 1 ? "" : "s", viewAllLinkHref));
            }
        }

        return html.toString();
    }

    private String renderOneUpcoming(UpcomingListing listing) {
        String mapLink = (listing.latitude() != null && listing.longitude() != null)
                ? " &nbsp;·&nbsp; <a href='%s/auction-scout/?lat=%s&lng=%s&zoom=16'>View map →</a>"
                .formatted(appBaseUrl, listing.latitude(), listing.longitude())
                : "";

        return """
                <div class='listing'><div class='addr'>%s</div><div class='meta'>%s</div>
                <a href='%s'>View listing →</a>%s</div>
                """.formatted(
                escape(listing.address()),
                listing.auctionDateTime().format(LISTING_META),
                listing.sourceUrl(),
                mapLink
        );
    }

    private String renderChanges(List<ChangedListing> changes, boolean truncate, String viewAllLinkHref) {
        if (changes.isEmpty()) {
            return "<p class='empty'>No status changes to report this week.</p>";
        }

        // Group by address so a property with multiple events in this
        // window (e.g. a cancellation producing both a status_change AND
        // a date_change) renders as one row with combined labels, not one
        // row per event -- a subscriber seeing the same address twice in
        // one digest reads as a bug even though the underlying data isn't
        // wrong. LinkedHashMap preserves the query's detected_at DESC
        // ordering for whichever address is encountered first.
        java.util.Map<String, List<ChangedListing>> byAddress = new java.util.LinkedHashMap<>();
        for (ChangedListing change : changes) {
            byAddress.computeIfAbsent(change.address(), k -> new java.util.ArrayList<>()).add(change);
        }

        // Each address is bucketed into exactly one of these four
        // categories, so "show first 5, +N more" can be applied per
        // category rather than to one big mixed list. This is also the
        // display order: New first, then how things are shifting
        // (Date/Status), Removed last.
        List<String> newRows = new java.util.ArrayList<>();
        List<String> dateChangeRows = new java.util.ArrayList<>();
        List<String> statusChangeRows = new java.util.ArrayList<>();
        List<String> removedRows = new java.util.ArrayList<>();

        for (List<ChangedListing> group : byAddress.values()) {
            ChangedListing first = group.get(0);
            boolean wasNew = group.stream().anyMatch(c -> "first_seen".equals(c.eventType()));
            boolean wasRemoved = group.stream().anyMatch(c -> "disappeared".equals(c.eventType()));

            // Appeared and vanished within the same digest window: the
            // subscriber never had a real chance to see this listing, so
            // reporting it as "Removed" (or anything else) is noise, not
            // information. Skip the property entirely rather than
            // showing any tag for it.
            if (wasNew && wasRemoved) {
                continue;
            }

            // A brand-new listing that's both far out (>30 days) and
            // hasn't accumulated at least 7 days of observed history yet
            // is suppressed from the "New" announcement — see FAR_OUT_DAYS
            // comment above for the full reasoning.
            if (wasNew && !wasRemoved && first.auctionDateTime() != null
                    && first.firstSeenAt() != null && first.lastSeenAt() != null) {
                boolean farOut = first.auctionDateTime().isAfter(LocalDateTime.now().plusDays(FAR_OUT_DAYS));
                boolean notEnoughHistoryYet = first.lastSeenAt().isBefore(first.firstSeenAt().plusDays(MIN_HISTORY_DAYS));
                if (farOut && notEnoughHistoryYet) {
                    continue;
                }
            }

            String dateText = first.auctionDateTime() != null
                    ? first.auctionDateTime().format(LISTING_META)
                    : "date unknown";

            if (wasRemoved) {
                removedRows.add(changeRow(first, dateText, "<span class='tag'>%s</span>".formatted(escape("Removed"))));
            } else if (wasNew) {
                newRows.add(changeRow(first, dateText, "<span class='tag'>%s</span>".formatted(escape("New"))));
            } else {
                // Raw pass-through: event_type + new_value verbatim, no
                // categorization beyond the bucket assignment below.
                String labels = group.stream()
                        .map(change -> "%s: %s".formatted(change.eventType(), nullToDash(change.newValue())))
                        .distinct()
                        .map(DigestService::escape)
                        .map(l -> "<span class='tag'>%s</span>".formatted(l))
                        .collect(java.util.stream.Collectors.joining(" "));
                String row = changeRow(first, dateText, labels);

                // An address can technically span more than one non-New/
                // Removed event type in the same window (e.g. a
                // postponement that also moved the date). date_change
                // wins the bucket assignment when both are present --
                // deterministic, and keeps a single address from
                // appearing in two sections at once.
                boolean hasDateChange = group.stream().anyMatch(c -> "date_change".equals(c.eventType()));
                if (hasDateChange) {
                    dateChangeRows.add(row);
                } else {
                    // status_change (postponed/canceled/sold) and
                    // price_change both land here — price_change is rare
                    // enough not to warrant its own section.
                    statusChangeRows.add(row);
                }
            }
        }

        StringBuilder html = new StringBuilder();
        boolean anyTruncated = false;
        anyTruncated |= appendChangeSection(html, "New Listings", newRows, truncate);
        anyTruncated |= appendChangeSection(html, "Date Changes", dateChangeRows, truncate);
        anyTruncated |= appendChangeSection(html, "Status Changes", statusChangeRows, truncate);
        anyTruncated |= appendChangeSection(html, "Removed", removedRows, truncate);

        if (html.isEmpty()) {
            return "<p class='empty'>No status changes to report this week.</p>";
        }

        if (anyTruncated) {
            html.append("<p style='margin-top:12px;'><a href='%s'>View all changes →</a></p>\n"
                    .formatted(viewAllLinkHref));
        }

        return html.toString();
    }

    private String changeRow(ChangedListing listing, String dateText, String labels) {
        return "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>\n"
                .formatted(escape(listing.address()), dateText, labels, changeLinkCell(listing));
    }

    /**
     * "View listing" (always, if within window) plus "View map" (only
     * if the property has coordinates) -- same link shapes as
     * renderOneUpcoming(), reused here rather than duplicated logic
     * diverging over time. Empty string (no link at all) if the
     * auction is more than CHANGE_LINK_WINDOW_DAYS out, or dateless --
     * a change on something months away, or with no date at all, isn't
     * something to act on right now.
     */
    private String changeLinkCell(ChangedListing listing) {
        if (listing.auctionDateTime() == null) {
            return "";
        }
        boolean withinWindow = listing.auctionDateTime().isBefore(LocalDateTime.now().plusDays(NEW_LISTING_LINK_WINDOW_DAYS ));
        if (!withinWindow) {
            return "";
        }

        String mapLink = (listing.latitude() != null && listing.longitude() != null)
                ? " &nbsp;·&nbsp; <a href='%s/auction-scout/?lat=%s&lng=%s&zoom=16'>View map →</a>"
                .formatted(appBaseUrl, listing.latitude(), listing.longitude())
                : "";

        return "<a href='%s'>View listing →</a>%s".formatted(listing.sourceUrl(), mapLink);
    }

    /**
     * Renders one change subsection (mini heading + table), capped at
     * MAX_CHANGES_PER_BUCKET rows when truncate is true, with a "+N
     * more" note appended if any rows were cut. Renders nothing (and
     * returns false) if rows is empty — no point in a heading over an
     * empty table. Returns whether this section was actually truncated,
     * so the caller knows whether to show the overall "View all" link.
     */
    private boolean appendChangeSection(StringBuilder html, String heading, List<String> rows, boolean truncate) {
        if (rows.isEmpty()) {
            return false;
        }

        int shown = truncate ? Math.min(MAX_CHANGES_PER_BUCKET, rows.size()) : rows.size();
        int remaining = rows.size() - shown;

        html.append("<div class='day-header'>").append(escape(heading)).append("</div>\n");
        html.append("<table class='status-table'>\n");
        for (int i = 0; i < shown; i++) {
            html.append(rows.get(i));
        }
        html.append("</table>\n");

        if (remaining > 0) {
            html.append("<p class='empty'>+%d more</p>\n".formatted(remaining));
        }

        return remaining > 0;
    }

    private static String nullToDash(String s) {
        return (s == null || s.isBlank()) ? "—" : s;
    }

    private static String escape(String s) {
        if (s == null) return "";
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;");
    }
}