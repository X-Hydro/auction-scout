package com.oncoord.auctionscout.digest;

import com.oncoord.auctionscout.properties.PropertyDigestRepository;
import com.oncoord.auctionscout.properties.PropertyDigestRepository.ChangedListing;
import com.oncoord.auctionscout.properties.PropertyDigestRepository.UpcomingListing;
import com.oncoord.auctionscout.subscriber.SubscriberRepository;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

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
 * an email) and, later, the scheduled weekly email job (iterating over
 * every active subscriber directly, no HTTP involved at all) should
 * call this same method rather than duplicating the
 * "look up states, then render" logic in two places.
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

    private final PropertyDigestRepository repository;
    private final SubscriberRepository subscribers;
    private final String appBaseUrl;

    public DigestService(PropertyDigestRepository repository,
                         SubscriberRepository subscribers,
                         @Value("${auctionscout.app.base-url}") String appBaseUrl) {
        this.repository = repository;
        this.subscribers = subscribers;
        this.appBaseUrl = appBaseUrl;
    }

    /**
     * Looks up the subscriber's saved states and renders their digest.
     * The one method both the web preview and (later) the scheduled
     * email sender should call — neither should reach into
     * SubscriberRepository directly and duplicate this lookup.
     */
    public String renderForSubscriber(String email, OffsetDateTime changesSince) {
        List<String> states = subscribers.getStates(email);
        return render(states, changesSince);
    }

    /**
     * @param changesSince cutoff for the "what changed" section — until
     *                      per-subscriber "last digest sent" tracking
     *                      exists (scheduling work, not built yet),
     *                      callers should pass something reasonable
     *                      like "7 days ago" rather than a real
     *                      per-subscriber value.
     */
    public String render(List<String> states, OffsetDateTime changesSince) {
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
                renderUpcoming(upcoming),
                renderChanges(changes)
        );
    }

    private String renderUpcoming(List<UpcomingListing> upcoming) {
        StringBuilder html = new StringBuilder();
        String currentDay = null;
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
            if (!day.equals(currentDay)) {
                currentDay = day;
                html.append("<div class='day-header'>").append(day).append("</div>\n");
            }

            String mapLink = (listing.latitude() != null && listing.longitude() != null)
                    ? " &nbsp;·&nbsp; <a href='%s/auction-scout/?lat=%s&lng=%s&zoom=16'>View map →</a>"
                    .formatted(appBaseUrl, listing.latitude(), listing.longitude())
                    : "";

            html.append("""
                    <div class='listing'><div class='addr'>%s</div><div class='meta'>%s</div>
                    <a href='%s'>View listing →</a>%s</div>
                    """.formatted(
                    escape(listing.address()),
                    listing.auctionDateTime().format(LISTING_META),
                    listing.sourceUrl(),
                    mapLink
            ));
        }
        return html.isEmpty()
                ? "<p class='empty'>No auctions in the next 7 days for your selected states.</p>"
                : html.toString();
    }

    private String renderChanges(List<ChangedListing> changes) {
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

        StringBuilder rows = new StringBuilder("<table class='status-table'>\n");
        boolean anyRows = false;
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
            // (last_seen_at is still within 7 days of first_seen_at)
            // matches the profile of listings that show up and vanish
            // within days -- suppress the "New" announcement for these.
            // This is a broader bar than "never reconfirmed at all": a
            // listing rescraped once or twice within just a couple days
            // still hasn't demonstrated much, so it's held to the same
            // standard as one that's been seen only once. Using actual
            // observed-history length rather than a fixed calendar cutoff
            // off first_seen_at is deliberately conservative: since
            // scrapes run frequently, most real listings accumulate 7
            // days of history quickly and pass through untouched; only
            // genuinely short-lived listings get caught. NOTE: first_seen
            // only fires once and the digest window only looks back 7
            // days, so a listing suppressed here does NOT get
            // re-announced later once it accumulates enough history -- it
            // simply never gets a "New" tag at all if that happens after
            // this digest already ran. Accepted tradeoff per explicit
            // decision; revisit if under-reporting becomes a problem
            // (would need a persisted "already announced" flag instead
            // of relying solely on the one-shot first_seen event).
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

            // Raw pass-through: event_type + new_value verbatim, no
            // categorization -- except two hardcoded label overrides.
            // "first_seen" -> "New" reads better than the raw name.
            // "disappeared" -> "Removed": the DB/internal event_type
            // stays "disappeared", but nothing subscriber-facing should
            // say that -- we don't actually know what happened (sold,
            // cancelled, a scraper miss are all indistinguishable from
            // this event alone), and "Removed" is the neutral, external
            // term for "no longer tracked" without implying a cause.
            String labels;
            if (wasRemoved) {
                // "Removed" wins outright over any other event in the
                // same window (e.g. a status_change a few days earlier)
                // -- avoids a contradictory-looking combination of tags.
                labels = "<span class='tag'>%s</span>".formatted(escape("Removed"));
            } else {
                labels = group.stream()
                        .map(change -> "first_seen".equals(change.eventType())
                                ? "New"
                                : "%s: %s".formatted(change.eventType(), nullToDash(change.newValue())))
                        .distinct()
                        .map(DigestService::escape)
                        .map(l -> "<span class='tag'>%s</span>".formatted(l))
                        .collect(java.util.stream.Collectors.joining(" "));
            }

            rows.append("<tr><td>%s</td><td>%s</td><td>%s</td></tr>\n".formatted(
                    escape(first.address()), dateText, labels));
            anyRows = true;
        }
        if (!anyRows) {
            return "<p class='empty'>No status changes to report this week.</p>";
        }
        rows.append("</table>");
        return rows.toString();
    }

    private static String nullToDash(String s) {
        return (s == null || s.isBlank()) ? "—" : s;
    }

    private static String escape(String s) {
        if (s == null) return "";
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;");
    }
}