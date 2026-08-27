package com.oncoord.auctionscout.digest;

import com.oncoord.auctionscout.notification.NotificationRepository;
import com.oncoord.auctionscout.properties.PropertyDigestRepository;
import com.oncoord.auctionscout.properties.PropertyDigestRepository.ChangedListing;
import com.oncoord.auctionscout.properties.PropertyDigestRepository.UpcomingListing;
import com.oncoord.auctionscout.saved.SavedPropertiesRepository;
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
import java.util.Set;
import java.util.stream.Collectors;

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

    // How long a digest's "view all" links stay live without a login
    // step -- see com.oncoord.auth.common.TokenTtl.VIEW_TOKEN_TTL_MILLIS,
    // the single shared source of truth StatusController and
    // PreferencesController both check this token against.

    // See filterActiveListings() for how these two combine.
    private static final int ACTIVE_LISTING_CAP_DAYS = 30;
    private static final int URGENCY_WAIVER_DAYS = 7;

    // "Upcoming Auctions" section on the saved-property
    // alert email specifically -- narrower than the weekly digest's own
    // Upcoming Auctions section (ACTIVE_LISTING_CAP_DAYS=30). Distinct
    // constant from URGENCY_WAIVER_DAYS even though both are 7 days
    // today -- they mean different things (one's a seasoning waiver,
    // this one's the actual display window) and shouldn't drift
    // together just because they happen to match right now.
    private static final int SAVED_ALERT_UPCOMING_WINDOW_DAYS = 7;

    // status_change values meaning the auction won't happen as listed --
    // substring match since the pipeline's status text isn't a
    // controlled vocabulary (e.g. cancelled/canceled both occur).
    private static final List<String> TERMINAL_STATUS_KEYWORDS =
            List.of("cancel", "sold", "third party", "3rd party");

    // status.html/map links are still built directly (states are public,
    // no login required) but now carry a reusable view token proving
    // entitlement (see statusUrl()) -- a read-only mechanism, distinct
    // from preferences.html's link below, which needs a real one-time
    // login token since it's a write surface (see buildAutoLoginLink()).
    private static final String PREFERENCES_LINK_PLACEHOLDER = "{{PREFERENCES_LINK}}";

    private final PropertyDigestRepository repository;
    private final SubscriberRepository subscribers;
    private final NotificationRepository notifications;
    private final SavedPropertiesRepository savedProperties;
    private final TokenService tokenService;
    private final String appBaseUrl;

    public DigestService(PropertyDigestRepository repository,
                         SubscriberRepository subscribers,
                         NotificationRepository notifications,
                         SavedPropertiesRepository savedProperties,
                         TokenService tokenService,
                         @Value("${auctionscout.app.base-url}") String appBaseUrl) {
        this.repository = repository;
        this.subscribers = subscribers;
        this.notifications = notifications;
        this.savedProperties = savedProperties;
        this.tokenService = tokenService;
        this.appBaseUrl = appBaseUrl;
    }

    /**
     * Property IDs this subscriber has saved, or empty if email is null
     * (anonymous callers, unit tests). Saved properties bypass the
     * seasoning gate in filterActiveListings()/buildChangeGroups() --
     * see their javadocs -- so a subscriber never misses activity on
     * something they explicitly chose to track just because it's new
     * to the scraper.
     */
    private Set<Long> savedPropertyIdsFor(String email) {
        if (email == null) {
            return Set.of();
        }
        return savedProperties.findByEmail(email).stream()
                .map(SavedPropertiesRepository.SavedProperty::propertyId)
                .collect(Collectors.toSet());
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

    /** Issues a one-time token to post-login.html, redirecting to preferences.html. */
    private String buildPreferencesLink(String email) {
        return buildAutoLoginLink(email, "/auction-scout/preferences.html");
    }

    /**
     * Issues a one-time token to post-login.html, redirecting
     * to an arbitrary path once authenticated. Currently only
     * buildPreferencesLink() calls this -- kept as a general
     * redirectPath parameter rather than hardcoded to preferences.html
     * so any future write-surface email link (something that, unlike
     * status.html, needs a real session rather than a reusable view
     * token) can reuse it as-is. redirectPath may include its own query
     * string -- it's encoded whole as the outer redirect param's value,
     * so post-login.html gets it back intact.
     */
    private String buildAutoLoginLink(String email, String redirectPath) {
        // Must match VerifyController's normalization exactly -- tokens
        // are matched by exact subject string, so a mismatch here makes
        // every link "invalid or expired" even though it's brand new.
        String normalizedEmail = email.trim().toLowerCase();
        String rawToken = tokenService.issue(normalizedEmail);
        return appBaseUrl + "/auction-scout/post-login.html#email="
                + URLEncoder.encode(normalizedEmail, StandardCharsets.UTF_8)
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
     *              unit test). Always gates the "Removed" bucket
     *              against notification history, regardless of
     *              truncate -- null email means nothing is ever shown
     *              as Removed. This applies equally to the real weekly
     *              email (truncate=true) and the dead, subscriber-
     *              authenticated /status page (truncate=false) -- both
     *              represent a specific, identified subscriber, unlike
     *              the fully anonymous status.html dashboard, which
     *              never gates at all (see renderAsData/buildChangeGroups).
     */
    public String render(String email, List<String> states, OffsetDateTime changesSince, boolean truncate) {
        LocalDateTime now = LocalDateTime.now();
        Set<Long> savedPropertyIds = savedPropertyIdsFor(email);
        List<UpcomingListing> upcoming = filterActiveListings(repository.findActive(states, now), savedPropertyIds);
        List<ChangedListing> changes = repository.findRecentChanges(states, changesSince);

        // Only mint a view token for a real send (truncate=true, real
        // recipient email) -- the dead /status page (truncate=false)
        // already has its own session, same reasoning as
        // buildPreferencesLink's truncate check just above.
        String viewToken = (email != null && truncate) ? tokenService.issue(email) : null;

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
                <div class='header'><h1>AuctionScout - Auction Watch</h1><p>Weekly update — %s</p></div>
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
                renderUpcoming(upcoming, truncate, statusUrl(states, viewToken)),
                renderChanges(buildChangeGroups(changes, email, true, savedPropertyIds), truncate, statusUrl(states, viewToken)),
                PREFERENCES_LINK_PLACEHOLDER,
                PREFERENCES_LINK_PLACEHOLDER
        );
    }

    /**
     * Renders the daily saved-property alert email -- New Listings,
     * Date Changes, and Removed sections, for an explicit list of
     * property IDs rather than a subscriber's state list. Reuses
     * buildChangeGroups()/changeRow()/appendChangeSection() as-is:
     * since the caller (SavedPropertyAlertService) sources its input
     * from findRecentChangesForProperties(), which restricts to
     * date_change/disappeared/status_change/first_seen events, the
     * shared grouping logic naturally never produces a generic "Status
     * Changes" group from this input (status_change only ever
     * contributes to "Removed" here) -- no extra category filtering
     * needed for that one. "New" groups DO show up now (first_seen is
     * in scope -- see PropertyDigestRepository.findRecentChangesForProperties()
     * javadoc) and are bucketed separately in wrapSavedPropertyAlert().
     * Always gates Removed against notification history (this is
     * always a real email send) -- see buildChangeGroups.
     *
     * @return null if there's nothing to report -- caller should skip
     *         sending (and skip recording a notification) in that case
     */
    public String renderSavedPropertyAlert(String email, List<Long> propertyIds, OffsetDateTime since) {
        List<ChangedListing> changes = repository.findRecentChangesForProperties(propertyIds, since);
        List<ChangeGroup> groups = buildChangeGroups(changes, email, true, Set.copyOf(propertyIds));
        if (groups.isEmpty()) {
            return null;
        }
        return wrapSavedPropertyAlert(email, groups, false, null);
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
    public String renderSavedPropertyAlertForTest(String email, List<Long> propertyIds, OffsetDateTime since,
                                                  OffsetDateTime lastRealSentAt) {
        List<ChangedListing> changes = repository.findRecentChangesForProperties(propertyIds, since);
        List<ChangeGroup> groups = buildChangeGroups(changes, email, true, Set.copyOf(propertyIds));
        return wrapSavedPropertyAlert(email, groups, true, lastRealSentAt);
    }

    private String wrapSavedPropertyAlert(String email, List<ChangeGroup> groups, boolean isTest, OffsetDateTime lastRealSentAt) {
        List<String> newRows = new java.util.ArrayList<>();
        List<String> dateChangeRows = new java.util.ArrayList<>();
        List<String> removedRows = new java.util.ArrayList<>();
        for (ChangeGroup g : groups) {
            String dateText = g.listing().auctionDateTime() != null
                    ? g.listing().auctionDateTime().format(LISTING_META)
                    : "date unknown";
            String labelsHtml = g.labels().stream()
                    .map(l -> "<span class='tag'>%s</span>".formatted(escape(l)))
                    .collect(java.util.stream.Collectors.joining(" "));
            // Test-send only: label each row against the subscriber's
            // last REAL saved-property-alert send (see
            // SavedPropertyAlertService.sendTestAlert()) so a repeat
            // test send doesn't read as "still not fixed" -- a row is
            // "Already sent" if this exact change was detected before
            // that real cutoff (so a real alert would already have
            // covered it), "New since last alert" otherwise. No prior
            // real send at all (lastRealSentAt null) means everything
            // is necessarily new.
            if (isTest) {
                boolean alreadySent = lastRealSentAt != null
                        && g.listing().detectedAt() != null
                        && !g.listing().detectedAt().isAfter(lastRealSentAt);
                String testTag = alreadySent
                        ? "<span class='tag tag-already-sent'>Already sent</span>"
                        : "<span class='tag tag-new-change'>New since last alert</span>";
                labelsHtml = testTag + " " + labelsHtml;
            }
            String row = changeRow(g.listing(), dateText, labelsHtml, g.category());
            switch (g.category()) {
                case "New" -> newRows.add(row);
                case "Removed" -> removedRows.add(row);
                default -> dateChangeRows.add(row);
            }
        }

        StringBuilder sections = new StringBuilder();
        appendChangeSection(sections, "New Listings", newRows, false);
        appendChangeSection(sections, "Date Changes", dateChangeRows, false);
        appendChangeSection(sections, "Removed", removedRows, false);
        if (sections.isEmpty()) {
            sections.append("<p class='empty'>No recent updates on your saved properties.</p>");
        }

        String greetingName = escape(subscribers.findUsernameByEmail(email).filter(s -> !s.isBlank()).orElse("there"));

        String preferencesLink = buildPreferencesLink(email);
        List<String> states = subscribers.getStates(email);
        String dashboardLink = statusUrl(states, tokenService.issue(email));

        // Second section: auctions in the subscriber's selected states
        // happening in the next SAVED_ALERT_UPCOMING_WINDOW_DAYS days --
        // independent of which properties they've saved. Reuses
        // filterActiveListings() (terminal-status exclusion, seasoning
        // rules) so this doesn't duplicate that logic or drift from the
        // weekly digest's own Upcoming Auctions section; just narrows
        // the result to the tighter window afterward. savedPropertyIdsFor
        // lets any of the subscriber's saved properties bypass seasoning
        // here too, same as everywhere else that set is used.
        //
        // Omitted entirely -- not just when there are no states, but
        // also when there are states with nothing upcoming in them --
        // matching how New Listings/Date Changes/Removed above are each
        // silently skipped when empty (see appendChangeSection). An
        // "empty" section with no listings and no explanatory message
        // wouldn't tell the subscriber anything a missing section
        // doesn't already say.
        String upcomingSectionBlock = "";
        if (!states.isEmpty()) {
            LocalDateTime now = LocalDateTime.now();
            Set<Long> savedPropertyIdsForUpcoming = savedPropertyIdsFor(email);
            List<UpcomingListing> upcomingInStates = filterActiveListings(repository.findActive(states, now), savedPropertyIdsForUpcoming)
                    .stream()
                    .filter(l -> l.auctionDateTime().isBefore(now.plusDays(SAVED_ALERT_UPCOMING_WINDOW_DAYS)))
                    .toList();
            if (!upcomingInStates.isEmpty()) {
                String upcomingSectionHtml = renderUpcoming(upcomingInStates, false, dashboardLink);
                upcomingSectionBlock = "<div class='section'><h2>Upcoming Auctions (Next 7 Days)</h2>\n"
                        + upcomingSectionHtml + "\n</div>";
            }
        }

        // Visible only on the admin test-send path -- explains why a
        // repeated test send always shows the same changes (fixed
        // 90-day lookback, recordSent() deliberately never called; see
        // SavedPropertyAlertService.sendTestAlert()) so this doesn't
        // get mistaken for the dedup bug it's specifically NOT
        // affected by.
        String testBanner = isTest
                ? "<div class='test-banner'>⚠️ TEST SEND — this always checks the last 90 days " +
                "and does not mark anything as seen, so re-running it will keep showing the same " +
                "changes (see each row's Already sent/New since last alert tag). This does not " +
                "reflect what a real subscriber will receive.</div>"
                : "";

        return """
            <html><head><base target="_top"><style>
                body { font-family: -apple-system, Helvetica, Arial, sans-serif; color: #1a1a1a; margin:0; padding:0; background:#f4f4f4; }
                .container { max-width: 640px; margin: 0 auto; background:#ffffff; }
                .header { background:#1a3a5c; color:#ffffff; padding:24px 32px; }
                .header h1 { margin:0; font-size:20px; }
                .header p { margin:4px 0 0; font-size:13px; opacity:0.85; }
                .test-banner { background:#fff8e1; border-bottom:1px solid #f0d878; color:#7a5c00; font-size:12px; font-weight:600; padding:10px 32px; }
                .section { padding:24px 32px; }
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
                .tag-already-sent { background:#eee; color:#888; }
                .tag-new-change { background:#dff6dd; color:#1a7d34; }
                .empty { color:#999; font-size:13px; font-style:italic; }
                .footer { padding:20px 32px; font-size:11px; color:#999; }
            </style></head><body><div class='container'>
            <div class='header'><h1>AuctionScout — Saved Property Update</h1><p>Recent changes on your saved properties</p></div>
            %s
            <div class='section'>
            <p>Hello %s,</p>
            %s
            <p style='margin-top:16px;'><a href='%s'>View AuctionScout dashboard →</a></p>
            </div>
            %s
            <div class='footer'>You're receiving this because one or more of your saved properties changed. <a href='%s'>Manage preferences</a>
            <p style='margin-top:8px;font-size:13px;color:#666;'>The preferences link above works automatically the first time you click it. If it's already been used, just log in normally from the <a href='%s'>AuctionScout</a> login page.</p>
            </div>
            </div></body></html>
            """.formatted(testBanner, greetingName, sections.toString(), dashboardLink, upcomingSectionBlock, preferencesLink, appBaseUrl + "/auction-scout/register.html");
    }

    /**
     * States are still public -- this stays a plain, bookmarkable URL
     * with no token if viewToken is null (anonymous callers, tests).
     * When present, viewToken carries no email/PII in the URL itself;
     * it's an opaque, unguessable pointer StatusController resolves
     * server-side via TokenService.peek(token, ttl) to grant the
     * subscriber's real entitlement (state count above the free cap)
     * without a login step.
     */
    private String statusUrl(List<String> states, String viewToken) {
        String stateParam = String.join(",", states);
        String url = appBaseUrl + "/auction-scout/status.html?states="
                + URLEncoder.encode(stateParam, StandardCharsets.UTF_8);
        return viewToken == null ? url : url + "&vt=" + URLEncoder.encode(viewToken, StandardCharsets.UTF_8);
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
     * seasoning/dedup/removal rules and assigned a bucket. Shared
     * between the HTML digest and the CSV export so both stay in sync —
     * see buildChangeGroups.
     */
    private record ChangeGroup(ChangedListing listing, String category, List<String> labels) {}

    /**
     * Groups changes by address, applies the seasoning/dedup rules, and
     * assigns each surviving address to a category (New, Date Changes,
     * Status Changes, Removed). This is the one place that logic
     * lives — both renderChanges (HTML) and the status.html/CSV data
     * path build off this same list, so a rule change here can't drift
     * between outputs.
     *
     * @param email subscriber this is being built for, or null. Only
     *              consulted when gateRemovedOnNotificationHistory is
     *              true — ignored otherwise.
     * @param gateRemovedOnNotificationHistory true for an actual email
     *              send (weekly digest, saved-property alert): a
     *              removal is only surfaced if this subscriber was
     *              already emailed about the property before it
     *              disappeared, so nobody gets an unsolicited "this
     *              thing you never knew about is gone" email. false
     *              for a live page view (status.html): removals show
     *              unconditionally, same as every other category —
     *              there's no equivalent risk of surprise when
     *              someone's actively looking at the page right now.
     * @param savedPropertyIds property IDs this subscriber has saved —
     *              these bypass the isSeasoned() check below (but not
     *              the removal-notification gate, and not the terminal-
     *              status/date-cap rules that live in
     *              filterActiveListings() instead). A saved property is
     *              something the subscriber explicitly chose to track,
     *              so "not enough scraper history yet" shouldn't hide
     *              activity on it the way it does for the general feed.
     */
    private List<ChangeGroup> buildChangeGroups(List<ChangedListing> changes, String email,
                                                boolean gateRemovedOnNotificationHistory,
                                                Set<Long> savedPropertyIds) {
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
            // Saved properties skip this gate entirely -- see
            // savedPropertyIds javadoc above.
            boolean isSaved = savedPropertyIds.contains(first.propertyId());
            if (!isSaved && !isSeasoned(first.auctionDateTime(), first.firstSeenAt(), first.lastSeenAt())) {
                continue;
            }

            // Appeared and vanished within the same window -- the
            // subscriber never had a chance to see it, so skip entirely.
            if (wasNew && wasRemoved) {
                continue;
            }

            if (wasRemoved) {
                if (gateRemovedOnNotificationHistory) {
                    // Only announce Removed if this subscriber was
                    // actually emailed before the removal was detected
                    // -- otherwise they're being told something
                    // disappeared that they never knew existed. Null
                    // email (no subscriber context) counts as "never
                    // emailed".
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
     * page both use this): dateless listings are always suppressed --
     * there's no date to show them under, saved or not; a terminal-
     * status listing (cancel/sold/third-party -- see isTerminalStatus(),
     * same check the Changes pipeline already uses) is suppressed
     * regardless of its date, since a stale future auction_datetime on
     * a cancelled listing is exactly the case this exists to catch, and
     * showing a saved-but-cancelled auction as "upcoming" would be
     * actively wrong, not just noisy; inside ACTIVE_LISTING_CAP_DAYS,
     * seasoning is required unless the auction is within
     * URGENCY_WAIVER_DAYS, in which case it's shown regardless --
     * better a little noise than missing something happening soon. A
     * property in savedPropertyIds bypasses BOTH the seasoning
     * requirement and the ACTIVE_LISTING_CAP_DAYS cap (same reasoning
     * as buildChangeGroups' param of the same name -- a subscriber who
     * explicitly saved something wants to see it regardless of how far
     * out it is) but NOT the dateless/terminal-status filters, which
     * aren't about distance or confidence at all.
     */
    private List<UpcomingListing> filterActiveListings(List<UpcomingListing> listings, Set<Long> savedPropertyIds) {
        LocalDateTime now = LocalDateTime.now();
        return listings.stream()
                .filter(l -> l.auctionDateTime() != null)
                .filter(l -> !isTerminalStatus(l.status()))
                .filter(l -> savedPropertyIds.contains(l.propertyId())
                        || l.auctionDateTime().isBefore(now.plusDays(ACTIVE_LISTING_CAP_DAYS)))
                .filter(l -> savedPropertyIds.contains(l.propertyId())
                        || isSeasoned(l.auctionDateTime(), l.firstSeenAt(), l.lastSeenAt())
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
                        escape(listing.address()), dateText, labels, changeLinkCell(listing, category));
    }

    /** "View listing"/"View map" links, or "Coming soon" past NEW_LISTING_LINK_WINDOW_DAYS (postponements make a link unreliable that far out), or nothing if dateless. */
    private String changeLinkCell(ChangedListing listing, String category) {
        if (listing.auctionDateTime() == null || "Removed".equals(category)) {
            return "";
        }

        if ("New".equals(category)) {
            boolean withinWindow = listing.auctionDateTime().isBefore(LocalDateTime.now().plusDays(NEW_LISTING_LINK_WINDOW_DAYS));
            if (!withinWindow) {
                return "<span class='empty'>Coming soon</span>";
            }
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
     * false), for a genuinely anonymous caller with no subscriber
     * context at all. StatusController's real /status/data endpoint
     * calls the email-aware overload below instead, once it's resolved
     * whichever subscriber (if any) the request belongs to -- this
     * overload remains for callers that truly have no email to pass.
     */
    public DigestData renderAsData(List<String> states, OffsetDateTime changesSince) {
        return renderAsData(states, null, changesSince);
    }

    /**
     * @param email subscriber this is being built for, or null for an
     *              anonymous/unauthenticated caller. StatusController's
     *              /status/data endpoint DOES resolve a subscriber email
     *              when a session token or view token is present (see
     *              its javadoc) and passes it through here -- this is
     *              what lets savedPropertyIdsFor(email) bypass the
     *              seasoning gate for that subscriber's saved properties
     *              on the status page, same as the weekly email. Only a
     *              genuinely anonymous visitor (no session, no view
     *              token) gets null, and therefore no bypass.
     *
     * Removed items are shown unconditionally on this path regardless
     * of whether email is present -- gateRemovedOnNotificationHistory
     * is hardcoded false below. The notification-history gate only
     * applies to actual email sends (the weekly digest and
     * saved-property alerts, via render()/renderSavedPropertyAlert());
     * see buildChangeGroups.
     */
    public DigestData renderAsData(List<String> states, String email, OffsetDateTime changesSince) {
        LocalDateTime now = LocalDateTime.now();

        Set<Long> savedPropertyIds = savedPropertyIdsFor(email);
        List<UpcomingListing> upcoming = filterActiveListings(repository.findActive(states, now), savedPropertyIds);
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
        List<ChangeRow> changeRows = buildChangeGroups(changes, email, /* gateRemovedOnNotificationHistory */ false, savedPropertyIds).stream()
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