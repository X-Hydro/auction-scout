package com.oncoord.auctionscout.digest;

import com.oncoord.auctionscout.notification.NotificationRepository;
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
 * email_preview.html — from live data in auctionscout.db. Shows
 * event_type/old_value/new_value verbatim rather than interpreting them
 * into a fixed vocabulary (see PropertyDigestRepository's javadoc).
 *
 * renderForSubscriber() is the shared entry point for both the status
 * page and the scheduled email job. `truncate` is how they differ:
 * false shows everything, true caps each section with "+N more" links.
 * See filterActiveListings() for which properties qualify to be shown
 * at all — that rule is identical for both.
 */
@Service
public class DigestService {

    private static final DateTimeFormatter DAY_HEADER = DateTimeFormatter.ofPattern("EEEE, MMMM d", Locale.US);
    private static final DateTimeFormatter LISTING_META = DateTimeFormatter.ofPattern("MM/dd 'at' h:mm a", Locale.US);

    // See isSeasoned(): a property needs this many days of confirmed
    // observation before it counts as trustworthy, not scraper noise.
    private static final int SEASONING_WINDOW_DAYS = 7;

    // Email-only caps (truncate=true); the status page always shows
    // everything. MAX_ACTIVE_LISTINGS_EMAIL is a TOTAL cap on the
    // Upcoming Auctions section, not per-day.
    private static final int MAX_ACTIVE_LISTINGS_EMAIL = 5;
    private static final int MAX_CHANGES_PER_BUCKET = 5;

    // A change only gets a "View listing/map" link if the auction is
    // within this many days — nothing to act on for one months out.
    private static final int NEW_LISTING_LINK_WINDOW_DAYS = 21;

    // See filterActiveListings() for how these two combine.
    private static final int ACTIVE_LISTING_CAP_DAYS = 30;
    private static final int URGENCY_WAIVER_DAYS = 7;

    // status_change values meaning the auction won't happen as listed --
    // substring match since the pipeline's status text isn't a
    // controlled vocabulary (e.g. cancelled/canceled both occur).
    private static final List<String> TERMINAL_STATUS_KEYWORDS =
            List.of("cancel", "sold", "third party", "3rd party");

    // status.html and the map are unauthenticated -- states are public,
    // so their links are built directly. preferences.html still needs a
    // session, so its link is the only one built via a magic-link token
    // (only renderForSubscriber() has an email to mint one for).
    private static final String PREFERENCES_LINK_PLACEHOLDER = "{{PREFERENCES_LINK}}";

    private final PropertyDigestRepository repository;
    private final SubscriberRepository subscribers;
    private final NotificationRepository notifications;
    private final TokenService tokenService;
    private final String appBaseUrl;

    public DigestService(PropertyDigestRepository repository,
                         SubscriberRepository subscribers,
                         NotificationRepository notifications,
                         TokenService tokenService,
                         @Value("${auctionscout.app.base-url}") String appBaseUrl) {
        this.repository = repository;
        this.subscribers = subscribers;
        this.notifications = notifications;
        this.tokenService = tokenService;
        this.appBaseUrl = appBaseUrl;
    }

    /**
     * Fills in the preferences-link placeholder render() leaves behind.
     * truncate=true (the real email) issues a fresh auto-login token,
     * since the recipient isn't already logged in; truncate=false (the
     * /status view, viewer already has a session) links plain
     * preferences.html instead, so viewing the page doesn't mint a
     * wasted single-use token on every load.
     */
    public String renderForSubscriber(String email, OffsetDateTime changesSince, boolean truncate) {
        List<String> states = subscribers.getStates(email);
        String html = render(email, states, changesSince, truncate);

        String preferencesLink = truncate
                ? buildPreferencesLink(email)
                : appBaseUrl + "/auction-scout/preferences.html";
        return html.replace(PREFERENCES_LINK_PLACEHOLDER, preferencesLink);
    }

    /** Issues a magic-link token to post-login.html, redirecting to preferences.html. */
    private String buildPreferencesLink(String email) {
        return buildAutoLoginLink(email, "/auction-scout/preferences.html");
    }

    /**
     * Issues a magic-link token to post-login.html, redirecting to an
     * arbitrary path once authenticated -- generalized out of
     * buildPreferencesLink() so other subscriber-only email links (e.g.
     * the saved-property alert's dashboard link) can use the same
     * pattern instead of a bare, unauthenticated URL. redirectPath may
     * include its own query string (e.g. "/auction-scout/status.html
     * ?states=NH,RI") -- it's encoded whole as the outer redirect
     * param's value, so post-login.html gets it back intact.
     */
    private String buildAutoLoginLink(String email, String redirectPath) {
        String rawToken = tokenService.issue(email);
        return appBaseUrl + "/auction-scout/post-login.html#email="
                + URLEncoder.encode(email, StandardCharsets.UTF_8)
                + "&token=" + URLEncoder.encode(rawToken, StandardCharsets.UTF_8)
                + "&redirect=" + URLEncoder.encode(redirectPath, StandardCharsets.UTF_8);
    }

    /**
     * @param changesSince cutoff for the "what changed" section. Until
     *                      per-subscriber "last sent" tracking exists,
     *                      pass something like "7 days ago".
     * @param truncate      true for email (cap sections, "+N more ->
     *                      view all" links); false to show everything.
     *                      Doesn't affect which properties qualify —
     *                      see filterActiveListings().
     */
    public String render(List<String> states, OffsetDateTime changesSince, boolean truncate) {
        return render(null, states, changesSince, truncate);
    }

    /**
     * @param email the subscriber this digest is for, or null (e.g. a
     *              unit test). Gates the "Removed" bucket against
     *              notification history — null means nothing is ever
     *              shown as Removed.
     */
    public String render(String email, List<String> states, OffsetDateTime changesSince, boolean truncate) {
        LocalDateTime now = LocalDateTime.now();
        List<UpcomingListing> upcoming = filterActiveListings(repository.findActive(states, now));
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
                <div class='section'><h2>Upcoming Auctions</h2>
                %s
                </div>
                <div class='section'><h2>Status Changes</h2>
                %s
                </div>
                <div class='footer'>You're receiving this because you subscribed to AuctionScout Auction Watch. <a href='%s'>Manage preferences</a> &middot; <a href='%s'>Unsubscribe</a></div>
                </div></body></html>
                """.formatted(
                now.format(DateTimeFormatter.ofPattern("MMMM d, yyyy", Locale.US)),
                renderUpcoming(upcoming, truncate, statusUrl(states)),
                renderChanges(buildChangeGroups(changes, email), truncate, statusUrl(states)),
                PREFERENCES_LINK_PLACEHOLDER,
                PREFERENCES_LINK_PLACEHOLDER
        );
    }

    /**
     * Renders the daily saved-property alert email -- Date Changes and
     * Removed sections only, for an explicit list of property IDs
     * rather than a subscriber's state list. Reuses
     * buildChangeGroups()/changeRow()/appendChangeSection() as-is:
     * since the caller (SavedPropertyAlertService) sources its input
     * from findRecentChangesForProperties(), which already restricts
     * to date_change/disappeared events, the shared grouping logic
     * naturally never produces "New" or generic "Status Changes"
     * groups from this input -- no extra category filtering needed
     * here, and the "Removed" everEmailed safety gate in
     * buildChangeGroups still applies untouched.
     *
     * @return null if there's nothing to report -- caller should skip
     *         sending (and skip recording a notification) in that case
     */
    public String renderSavedPropertyAlert(String email, List<Long> propertyIds, OffsetDateTime since) {
        List<ChangedListing> changes = repository.findRecentChangesForProperties(propertyIds, since);
        List<ChangeGroup> groups = buildChangeGroups(changes, email);
        if (groups.isEmpty()) {
            return null;
        }
        return wrapSavedPropertyAlert(email, groups);
    }

    /**
     * Test-only counterpart to renderSavedPropertyAlert() -- never
     * returns null. The real method correctly stays silent (and sends
     * nothing) when there's nothing to report; a test button needs the
     * opposite, since the whole point is confirming something lands in
     * the inbox. Falls back to a clear "no changes found" placeholder
     * rather than an empty digest, so a test send is never
     * indistinguishable from a broken one.
     */
    public String renderSavedPropertyAlertForTest(String email, List<Long> propertyIds, OffsetDateTime since) {
        List<ChangedListing> changes = repository.findRecentChangesForProperties(propertyIds, since);
        List<ChangeGroup> groups = buildChangeGroups(changes, email);
        return wrapSavedPropertyAlert(email, groups);
    }

    private String wrapSavedPropertyAlert(String email, List<ChangeGroup> groups) {
        List<String> dateChangeRows = new java.util.ArrayList<>();
        List<String> removedRows = new java.util.ArrayList<>();
        for (ChangeGroup g : groups) {
            String dateText = g.listing().auctionDateTime() != null
                    ? g.listing().auctionDateTime().format(LISTING_META)
                    : "date unknown";
            String labelsHtml = g.labels().stream()
                    .map(l -> "<span class='tag'>%s</span>".formatted(escape(l)))
                    .collect(java.util.stream.Collectors.joining(" "));
            String row = changeRow(g.listing(), dateText, labelsHtml, g.category());
            // Only these two categories can appear here -- see javadoc
            // on renderSavedPropertyAlert().
            if ("Removed".equals(g.category())) {
                removedRows.add(row);
            } else {
                dateChangeRows.add(row);
            }
        }

        StringBuilder sections = new StringBuilder();
        appendChangeSection(sections, "Date Changes", dateChangeRows, false);
        appendChangeSection(sections, "Removed", removedRows, false);
        if (sections.isEmpty()) {
            sections.append("<p class='empty'>No recent date changes or removals on your saved properties.</p>");
        }

        String preferencesLink = buildPreferencesLink(email);
        // Auto-login, not a bare statusUrl() link -- status.html's
        // multi-state view requires a session token to know this is a
        // real subscriber with an active subscription, not an
        // anonymous visitor capped to one state. A plain link here
        // would land them on the "Upgrade to view multiple states"
        // page despite already being one, which is exactly wrong for a
        // subscriber-only email like this one (unlike the weekly
        // digest's "view all" links, which are meant to also work as
        // bare, sharable URLs -- see statusUrl()'s own doc comment).
        String statesParam = String.join(",", subscribers.getStates(email));
        String statusRedirectPath = "/auction-scout/status.html?states="
                + URLEncoder.encode(statesParam, StandardCharsets.UTF_8);
        String dashboardLink = buildAutoLoginLink(email, statusRedirectPath);
        return """
                <html><head><base target="_top"><style>
                    body { font-family: -apple-system, Helvetica, Arial, sans-serif; color: #1a1a1a; margin:0; padding:0; background:#f4f4f4; }
                    .container { max-width: 640px; margin: 0 auto; background:#ffffff; }
                    .header { background:#1a3a5c; color:#ffffff; padding:24px 32px; }
                    .header h1 { margin:0; font-size:20px; }
                    .header p { margin:4px 0 0; font-size:13px; opacity:0.85; }
                    .section { padding:24px 32px; }
                    .day-header { font-size:13px; font-weight:600; color:#666; margin:16px 0 8px; text-transform:uppercase; letter-spacing:0.03em; }
                    table.status-table { width:100%%; border-collapse:collapse; font-size:13px; }
                    table.status-table td { padding:8px 4px; border-bottom:1px solid #f0f0f0; }
                    table.status-table a { color:#1a5c9c; text-decoration:none; }
                    .tag { display:inline-block; padding:2px 8px; border-radius:3px; font-size:11px; font-weight:600; background:#eef0f4; color:#3a4556; }
                    .empty { color:#999; font-size:13px; font-style:italic; }
                    .footer { padding:20px 32px; font-size:11px; color:#999; }
                </style></head><body><div class='container'>
                <div class='header'><h1>AuctionScout — Saved Property Update</h1><p>Something changed on one of your saved properties</p></div>
                <div class='section'>
                %s
                <p style='margin-top:16px;'><a href='%s'>View on your AuctionScout dashboard →</a></p>
                </div>
                <div class='footer'>You're receiving this because one or more of your saved properties changed. <a href='%s'>Manage preferences</a>
                <p style='margin-top:8px;font-size:13px;color:#666;'>The links above sign you in automatically and each works once. If a link's already been used, just log in normally from the <a href='%s'>AuctionScout</a> login page.</p>
                </div>
                </div></body></html>
                """.formatted(sections.toString(), dashboardLink, preferencesLink, appBaseUrl + "/auction-scout/register.html");
    }

    /** status.html and the map are unauthenticated -- states aren't sensitive, so this is a plain, bookmarkable URL. */
    private String statusUrl(List<String> states) {
        String stateParam = String.join(",", states);
        return appBaseUrl + "/auction-scout/status.html?states="
                + URLEncoder.encode(stateParam, StandardCharsets.UTF_8);
    }

    private String renderUpcoming(List<UpcomingListing> upcoming, boolean truncate, String viewAllLinkHref) {
        // Cap applies to the WHOLE list before day-grouping, not per-day
        // (see MAX_ACTIVE_LISTINGS_EMAIL) -- already ordered by
        // auction_datetime ascending, see repository.
        List<UpcomingListing> shown = truncate && upcoming.size() > MAX_ACTIVE_LISTINGS_EMAIL
                ? upcoming.subList(0, MAX_ACTIVE_LISTINGS_EMAIL)
                : upcoming;
        int remaining = upcoming.size() - shown.size();

        java.util.Map<String, List<UpcomingListing>> byDay = new java.util.LinkedHashMap<>();
        for (UpcomingListing listing : shown) {
            if (listing.auctionDateTime() == null) {
                continue; // defensive -- filterActiveListings already drops these upstream
            }
            String day = listing.auctionDateTime().format(DAY_HEADER);
            byDay.computeIfAbsent(day, k -> new java.util.ArrayList<>()).add(listing);
        }

        if (byDay.isEmpty()) {
            return "<p class='empty'>No active auctions for your selected states.</p>";
        }

        StringBuilder html = new StringBuilder();
        for (var entry : byDay.entrySet()) {
            html.append("<div class='day-header'>").append(entry.getKey()).append("</div>\n");
            for (UpcomingListing listing : entry.getValue()) {
                html.append(renderOneUpcoming(listing));
            }
        }

        if (remaining > 0) {
            html.append("<p class='empty'>+%d more auction%s — <a href='%s'>view all →</a></p>\n"
                    .formatted(remaining, remaining == 1 ? "" : "s", viewAllLinkHref));
        }

        return html.toString();
    }

    private String renderOneUpcoming(UpcomingListing listing) {
        String mapUrl = mapUrl(listing.latitude(), listing.longitude());
        String mapLink = mapUrl.isEmpty() ? "" : " &nbsp;·&nbsp; <a href='%s'>View map →</a>".formatted(mapUrl);

        return """
                <div class='listing' data-state='%s' data-date='%s'><div class='addr'>%s</div><div class='meta'>%s</div>
                <a href='%s'>View listing →</a>%s</div>
                """.formatted(
                escape(listing.state()),
                listing.auctionDateTime(),
                escape(listing.address()),
                listing.auctionDateTime().format(LISTING_META),
                listing.sourceUrl(),
                mapLink
        );
    }

    /**
     * One address's worth of change activity, already put through the
     * seasoning/dedup/everEmailed rules and assigned a bucket. Shared
     * between the HTML digest and the CSV export so both stay in sync —
     * see buildChangeGroups.
     */
    private record ChangeGroup(ChangedListing listing, String category, List<String> labels) {}

    /**
     * Groups changes by address, applies the seasoning/dedup/everEmailed
     * rules, and assigns each surviving address to a category (New,
     * Date Changes, Status Changes, Removed). This is the one place
     * that logic lives — both renderChanges (HTML) and changesCsv build
     * off this same list, so a rule change here can't drift between the
     * two outputs.
     */
    private List<ChangeGroup> buildChangeGroups(List<ChangedListing> changes, String email) {
        // Group by address so a property with multiple events this
        // window collapses into one entry with combined labels, not one
        // entry per event.
        java.util.Map<String, List<ChangedListing>> byAddress = new java.util.LinkedHashMap<>();
        for (ChangedListing change : changes) {
            byAddress.computeIfAbsent(change.address(), k -> new java.util.ArrayList<>()).add(change);
        }

        // Display order: New, then Date/Status changes, then Removed.
        List<ChangeGroup> newGroups = new java.util.ArrayList<>();
        List<ChangeGroup> dateChangeGroups = new java.util.ArrayList<>();
        List<ChangeGroup> statusChangeGroups = new java.util.ArrayList<>();
        List<ChangeGroup> removedGroups = new java.util.ArrayList<>();

        for (List<ChangedListing> group : byAddress.values()) {
            ChangedListing first = group.get(0);
            boolean wasNew = group.stream().anyMatch(c -> "first_seen".equals(c.eventType()));
            boolean wasRemoved = group.stream().anyMatch(DigestService::isRemovalEvent);
            // date_change/price_change outrank a bare "New" tag when both
            // land in the same window. status_change isn't listed here --
            // it's already folded into wasRemoved or noise.
            boolean hasOtherChangeType = group.stream().anyMatch(c ->
                    "date_change".equals(c.eventType()) || "price_change".equals(c.eventType()));

            // Applied uniformly (New/Date/Removed alike), not just to
            // New -- otherwise a date_change riding alongside first_seen
            // could bypass a narrower check, and an unseasoned property
            // could later surface as an unexplained Removed in a
            // different window where its first_seen isn't in view.
            if (!isSeasoned(first.auctionDateTime(), first.firstSeenAt(), first.lastSeenAt())) {
                continue;
            }

            // Appeared and vanished within the same window -- the
            // subscriber never had a chance to see it, so skip entirely.
            if (wasNew && wasRemoved) {
                continue;
            }

            if (wasRemoved) {
                // Only announce Removed if this subscriber was actually
                // emailed before the removal was detected -- otherwise
                // they're being told something disappeared that they
                // never knew existed. Null email (no subscriber context)
                // counts as "never emailed".
                OffsetDateTime disappearedAt = group.stream()
                        .filter(DigestService::isRemovalEvent)
                        .map(ChangedListing::detectedAt)
                        .filter(java.util.Objects::nonNull)
                        .max(OffsetDateTime::compareTo)
                        .orElse(null);
                boolean everEmailed = email != null && disappearedAt != null
                        && notifications.hasSentBefore(email, disappearedAt.toInstant().toEpochMilli());
                if (!everEmailed) {
                    continue;
                }
                removedGroups.add(new ChangeGroup(first, "Removed", List.of("Removed")));
            } else if (wasNew && !hasOtherChangeType) {
                newGroups.add(new ChangeGroup(first, "New", List.of("New")));
            } else {
                // Raw pass-through for whatever's left (price_change, or
                // any not-yet-recognized event type); date_change gets
                // special date-only formatting. first_seen, removal
                // events, and noise status_change are excluded --
                // handled above or suppressed as not actionable.
                List<String> labels = group.stream()
                        .filter(change -> !"first_seen".equals(change.eventType())
                                && !isRemovalEvent(change)
                                && !isNoiseStatusChange(change))
                        .map(DigestService::formatChangeLabel)
                        .distinct()
                        .toList();
                if (wasNew) {
                    labels = java.util.stream.Stream.concat(java.util.stream.Stream.of("New"), labels.stream()).toList();
                }
                if (labels.isEmpty()) {
                    // Nothing left after filtering noise -- skip rather
                    // than render an empty, unexplained row.
                    continue;
                }
                // date_change wins the bucket when combined with
                // price_change, so an address only appears in one place.
                boolean hasDateChange = group.stream().anyMatch(c -> "date_change".equals(c.eventType()));
                String category = hasDateChange ? "Date Changes" : "Status Changes";
                ChangeGroup g = new ChangeGroup(first, category, labels);
                if (hasDateChange) {
                    dateChangeGroups.add(g);
                } else {
                    statusChangeGroups.add(g);
                }
            }
        }

        List<ChangeGroup> all = new java.util.ArrayList<>();
        all.addAll(newGroups);
        all.addAll(dateChangeGroups);
        all.addAll(statusChangeGroups);
        all.addAll(removedGroups);
        return all;
    }

    /**
     * True once a listing has SEASONING_WINDOW_DAYS of confirmed
     * re-scraping (last_seen_at - first_seen_at), OR its auction is
     * close enough that the gate doesn't apply (see URGENCY_WAIVER_DAYS
     * in filterActiveListings). A missing timestamp fails open (treated
     * as seasoned) rather than hiding a listing over a bookkeeping gap.
     */
    private static boolean isSeasoned(LocalDateTime auctionDateTime, OffsetDateTime firstSeenAt, OffsetDateTime lastSeenAt) {
        if (auctionDateTime == null || firstSeenAt == null || lastSeenAt == null) {
            return true;
        }
        boolean farOut = auctionDateTime.isAfter(LocalDateTime.now().plusDays(SEASONING_WINDOW_DAYS));
        boolean notEnoughHistoryYet = lastSeenAt.isBefore(firstSeenAt.plusDays(SEASONING_WINDOW_DAYS));
        return !(farOut && notEnoughHistoryYet);
    }

    /**
     * The eligibility rule for "Upcoming Auctions" (email and status
     * page both use this): dateless listings are always suppressed;
     * nothing beyond ACTIVE_LISTING_CAP_DAYS out is shown regardless of
     * seasoning (too likely to be postponed before it matters); inside
     * that cap, seasoning is required unless the auction is within
     * URGENCY_WAIVER_DAYS, in which case it's shown regardless -- better
     * a little noise than missing something happening soon.
     */
    private List<UpcomingListing> filterActiveListings(List<UpcomingListing> listings) {
        LocalDateTime now = LocalDateTime.now();
        return listings.stream()
                .filter(l -> l.auctionDateTime() != null)
                .filter(l -> l.auctionDateTime().isBefore(now.plusDays(ACTIVE_LISTING_CAP_DAYS)))
                .filter(l -> isSeasoned(l.auctionDateTime(), l.firstSeenAt(), l.lastSeenAt())
                        || l.auctionDateTime().isBefore(now.plusDays(URGENCY_WAIVER_DAYS)))
                .toList();
    }

    private String renderChanges(List<ChangeGroup> groups, boolean truncate, String viewAllLinkHref) {
        // Each address lands in exactly one bucket (see buildChangeGroups),
        // so "+N more" applies per category.
        List<String> newRows = new java.util.ArrayList<>();
        List<String> dateChangeRows = new java.util.ArrayList<>();
        List<String> statusChangeRows = new java.util.ArrayList<>();
        List<String> removedRows = new java.util.ArrayList<>();

        for (ChangeGroup g : groups) {
            String dateText = g.listing().auctionDateTime() != null
                    ? g.listing().auctionDateTime().format(LISTING_META)
                    : "date unknown";
            String labelsHtml = g.labels().stream()
                    .map(l -> "<span class='tag'>%s</span>".formatted(escape(l)))
                    .collect(java.util.stream.Collectors.joining(" "));
            String row = changeRow(g.listing(), dateText, labelsHtml, g.category());
            switch (g.category()) {
                case "New" -> newRows.add(row);
                case "Date Changes" -> dateChangeRows.add(row);
                case "Removed" -> removedRows.add(row);
                default -> statusChangeRows.add(row);
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

    /**
     * True for a structural 'disappeared' event or a status_change whose
     * new value indicates the auction is over -- both render as
     * "Removed".
     */
    private static boolean isRemovalEvent(ChangedListing c) {
        return "disappeared".equals(c.eventType())
                || ("status_change".equals(c.eventType()) && isTerminalStatus(c.newValue()));
    }

    /**
     * status_change values that are neither terminal nor informative
     * (e.g. "active") -- just the property cycling through listing
     * states, not something to act on. Excluded from the digest.
     */
    private static boolean isNoiseStatusChange(ChangedListing c) {
        return "status_change".equals(c.eventType()) && !isTerminalStatus(c.newValue());
    }

    private static boolean isTerminalStatus(String rawStatus) {
        if (rawStatus == null) {
            return false;
        }
        String lower = rawStatus.toLowerCase(Locale.US);
        return TERMINAL_STATUS_KEYWORDS.stream().anyMatch(lower::contains);
    }

    /**
     * date_change gets date-only old -> new; everything else is raw
     * pass-through. Returns plain text (not HTML-escaped) so the CSV
     * export can use it as-is; the HTML renderer escapes at the point
     * it wraps each label in a <span class='tag'>.
     */
    private static String formatChangeLabel(ChangedListing change) {
        if ("date_change".equals(change.eventType())) {
            return "%s → %s".formatted(dateOnly(change.oldValue()), dateOnly(change.newValue()));
        }
        return "%s: %s".formatted(change.eventType(), nullToDash(change.newValue()));
    }

    /** Formats an ISO local datetime string down to just the date. Falls back to the raw value on parse failure. */
    private static String dateOnly(String rawIsoLocalDateTime) {
        if (rawIsoLocalDateTime == null || rawIsoLocalDateTime.isBlank()) {
            return "—";
        }
        try {
            return LocalDateTime.parse(rawIsoLocalDateTime).toLocalDate().toString();
        } catch (Exception e) {
            return rawIsoLocalDateTime;
        }
    }

    private String changeRow(ChangedListing listing, String dateText, String labels, String category) {
        // data-date is for client-side sorting; dateText is the
        // human-readable text shown in the cell.
        String isoDate = listing.auctionDateTime() != null ? listing.auctionDateTime().toString() : "";
        return "<tr data-state='%s' data-date='%s' data-category='%s'><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>\n"
                .formatted(escape(listing.state()), isoDate, escape(category),
                        escape(listing.address()), dateText, labels, changeLinkCell(listing));
    }

    /** "View listing"/"View map" links, or "Coming soon" past NEW_LISTING_LINK_WINDOW_DAYS (postponements make a link unreliable that far out), or nothing if dateless. */
    private String changeLinkCell(ChangedListing listing) {
        if (listing.auctionDateTime() == null) {
            return "";
        }
        boolean withinWindow = listing.auctionDateTime().isBefore(LocalDateTime.now().plusDays(NEW_LISTING_LINK_WINDOW_DAYS));
        if (!withinWindow) {
            return "<span class='empty'>Coming soon</span>";
        }

        String mapUrl = mapUrl(listing.latitude(), listing.longitude());
        String mapLink = mapUrl.isEmpty() ? "" : " &nbsp;·&nbsp; <a href='%s'>View map →</a>".formatted(mapUrl);

        return "<a href='%s'>View listing →</a>%s".formatted(listing.sourceUrl(), mapLink);
    }

    /**
     * Renders one change subsection, capped at MAX_CHANGES_PER_BUCKET
     * rows when truncated. Returns false (and renders nothing) if rows
     * is empty; otherwise returns whether it was truncated.
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

    /** Shared by both the HTML renderer and the JSON DTO builder so the map-link format only lives in one place. */
    private String mapUrl(Double latitude, Double longitude) {
        return (latitude != null && longitude != null)
                ? "%s/auction-scout/?lat=%s&lng=%s&zoom=16".formatted(appBaseUrl, latitude, longitude)
                : "";
    }

    /** One row of the "Auctions in the Next 7 Days" section, as data rather than pre-rendered HTML. */
    public record UpcomingRow(
            long propertyId,
            String state,
            String auctionDateTime, // ISO-8601 local date-time -- client-side sort key
            String dayLabel,        // e.g. "Monday, July 20"
            String dateLabel,       // e.g. "07/20 at 10:00 AM"
            String address,
            String sourceUrl,
            String mapUrl           // "" if not geocoded
    ) {}

    /** One row of the "Status Changes" section, as data rather than pre-rendered HTML. */
    public record ChangeRow(
            long propertyId,
            String state,
            String category,        // "New" | "Date Changes" | "Status Changes" | "Removed"
            String address,
            String auctionDateTime, // ISO-8601 local date-time, or "" if unknown -- client-side sort key
            String dateLabel,       // e.g. "07/20 at 10:00 AM" or "date unknown"
            List<String> labels,
            String sourceUrl,
            String mapUrl,          // "" if not geocoded
            boolean linkAvailable   // false once the auction's too far out for a link to stay accurate -- see the old changeLinkCell
    ) {}

    /**
     * @param upcoming the eligible set per filterActiveListings — same
     *                 set the email's "Upcoming Auctions" section uses;
     *                 this method just never truncates it.
     * @param changes  recent-activity digest.
     */
    public record DigestData(List<UpcomingRow> upcoming, List<ChangeRow> changes) {}

    /** Looks up the subscriber's states, then defers to renderAsData(). Kept for any per-email caller wanting structured data instead of HTML. */
    public DigestData renderForSubscriberAsData(String email, OffsetDateTime changesSince) {
        List<String> states = subscribers.getStates(email);
        return renderAsData(states, email, changesSince);
    }

    /**
     * Structured, untruncated equivalent of render(states, changesSince,
     * false) — status.html's data source. No email means the "Removed"
     * bucket is always empty (see buildChangeGroups: there's no
     * subscriber to check "were they ever shown this before it
     * disappeared").
     */
    public DigestData renderAsData(List<String> states, OffsetDateTime changesSince) {
        return renderAsData(states, null, changesSince);
    }

    private DigestData renderAsData(List<String> states, String email, OffsetDateTime changesSince) {
        LocalDateTime now = LocalDateTime.now();

        List<UpcomingListing> upcoming = filterActiveListings(repository.findActive(states, now));
        List<UpcomingRow> upcomingRows = upcoming.stream()
                .map(l -> new UpcomingRow(
                        l.propertyId(),
                        l.state(),
                        l.auctionDateTime().toString(),
                        l.auctionDateTime().format(DAY_HEADER),
                        l.auctionDateTime().format(LISTING_META),
                        l.address(),
                        l.sourceUrl(),
                        mapUrl(l.latitude(), l.longitude())
                ))
                .toList();

        List<ChangedListing> changes = repository.findRecentChanges(states, changesSince);
        List<ChangeRow> changeRows = buildChangeGroups(changes, email).stream()
                .map(g -> new ChangeRow(
                        g.listing().propertyId(),
                        g.listing().state(),
                        g.category(),
                        g.listing().address(),
                        g.listing().auctionDateTime() != null ? g.listing().auctionDateTime().toString() : "",
                        g.listing().auctionDateTime() != null ? g.listing().auctionDateTime().format(LISTING_META) : "date unknown",
                        g.labels(),
                        g.listing().sourceUrl(),
                        mapUrl(g.listing().latitude(), g.listing().longitude()),
                        g.listing().auctionDateTime() != null
                                && g.listing().auctionDateTime().isBefore(now.plusDays(NEW_LISTING_LINK_WINDOW_DAYS))
                ))
                .toList();

        return new DigestData(upcomingRows, changeRows);
    }
}