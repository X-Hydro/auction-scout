package com.oncoord.auctionscout.properties;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Click-time check for whether a source listing URL is still live.
 * Reads source_url straight from auctionscout.db by property_id (never
 * trusts a URL supplied by the caller -- an open URL-fetch proxy is an
 * SSRF hole).
 *
 * On a confirmed-dead link, appends a 'disappeared' auction_events row
 * -- the same category the Python pipeline's own structural
 * disappearance detection uses, and the only thing PropertyDigestRepository
 * actually excludes on (see its findUpcoming() javadoc). This is the one
 * write this service makes into auctionscout.db; everything else about
 * it stays read-only, same as PropertyDigestRepository.
 *
 * Uses dbManager.getJdbcTemplate() the same way PropertyDigestRepository
 * does -- I don't have PropertiesDbConnectionManager's actual source, so
 * this assumes that method is the only thing it exposes. If it has a
 * separate writable-connection accessor, swap the two inserts/reads
 * below to use that instead.
 */
@Service
public class LinkCheckService {

    private static final Logger log = LoggerFactory.getLogger(LinkCheckService.class);

    private static final Duration CACHE_TTL = Duration.ofMinutes(15);
    private static final Duration REQUEST_TIMEOUT = Duration.ofSeconds(5);

    private final PropertiesDbConnectionManager dbManager;
    private final HttpClient httpClient = HttpClient.newBuilder()
            .connectTimeout(REQUEST_TIMEOUT)
            .followRedirects(HttpClient.Redirect.NORMAL)
            .build();

    private final Map<Long, CachedResult> cache = new ConcurrentHashMap<>();

    public LinkCheckService(PropertiesDbConnectionManager dbManager) {
        this.dbManager = dbManager;
    }

    public record LinkCheckResult(boolean found, boolean alive) {}

    public LinkCheckResult check(long propertyId) {
        return check(propertyId, false);
    }

    public LinkCheckResult check(long propertyId, boolean bypassCache) {
        if (!bypassCache) {
            CachedResult cached = cache.get(propertyId);
            if (cached != null && !cached.isExpired()) {
                return cached.result;
            }
        }

        AuctionRef ref = findAuction(propertyId);
        if (ref == null) {
            return new LinkCheckResult(false, true); // unknown property -- fail open, nothing to check
        }

        boolean alive = pingUrl(ref.sourceUrl);
        if (!alive) {
            recordDisappeared(ref);
        }

        LinkCheckResult result = new LinkCheckResult(true, alive);
        cache.put(propertyId, new CachedResult(result, Instant.now().plus(CACHE_TTL)));
        return result;
    }

    private record AuctionRef(long auctionId, String sourceUrl, String status) {}

    private AuctionRef findAuction(long propertyId) {
        JdbcTemplate jdbc = dbManager.getJdbcTemplate();
        List<AuctionRef> rows = jdbc.query(
                "SELECT auction_id, source_url, status FROM auctions WHERE property_id = ?",
                (rs, rowNum) -> new AuctionRef(rs.getLong("auction_id"), rs.getString("source_url"), rs.getString("status")),
                propertyId
        );
        return rows.isEmpty() ? null : rows.get(0);
    }

    // Stock "no results" heading/text used by default WordPress search
    // templates (and copied by many small business themes) -- generic
    // across sources, not something we maintain per-source. A source
    // that never returns one of these just never gets caught by this
    // path; that's a false negative (same as today), not a false
    // positive, so it's safe to miss.
    private static final List<String> SOFT_404_PHRASES = List.of(
            "nothing found", "no results found", "no results were found",
            "we cant find what youre looking for", "no listings found",
            "no matching results", "content not found", "page not found"
    );

    /**
     * True only on a confirmed live page. False on a confirmed 404/410,
     * OR a 200 whose body looks like an empty search-results page (a
     * "soft 404" -- the server says OK, but the case number matched
     * nothing). Anything else -- timeout, 403/429 (source blocking us,
     * not removing the listing), 5xx, connection error -- is treated as
     * alive, since we'd rather occasionally send someone to a dead page
     * than wrongly mark a live auction removed over a network blip or a
     * source rate-limiting us.
     */
    private boolean pingUrl(String url) {
        if (url == null || url.isBlank()) return true;
        try {
            HttpRequest req = HttpRequest.newBuilder(URI.create(url))
                    .GET()
                    .timeout(REQUEST_TIMEOUT)
                    .header("User-Agent", "Mozilla/5.0 (compatible; AuctionScoutLinkCheck/1.0)")
                    .build();
            HttpResponse<String> res = httpClient.send(req, HttpResponse.BodyHandlers.ofString());

            int status = res.statusCode();
            boolean looksEmpty = status >= 200 && status < 400 && looksLikeEmptyResult(res.body());
            log.debug("link-check GET {} -> status={} bodyLen={} looksEmpty={} snippet={}",
                    url, status, res.body() == null ? 0 : res.body().length(), looksEmpty,
                    res.body() == null ? "" : res.body().substring(0, Math.min(res.body().length(), 3000)));

            if (status == 404 || status == 410) return false;
            if (status < 200 || status >= 400) return true; // ambiguous -- fail open

            return !looksEmpty;
        } catch (Exception e) {
            return true; // fail open
        }
    }

    private boolean looksLikeEmptyResult(String html) {
        if (html == null || html.isBlank()) return false;
        // No truncation: bloated <head> boilerplate and duplicated nav
        // markup (both common on WordPress sites, confirmed on the
        // Brock & Scott page that motivated this check) can easily push
        // the actual "no results" message past any reasonable prefix
        // window. The full scan is still a cheap String.contains pass
        // for a single on-demand click-time check.
        String normalized = html.toLowerCase(Locale.ROOT)
                .replace("’", "").replace("‘", "").replace("'", "")   // literal curly/straight apostrophes
                .replace("&rsquo;", "").replace("&lsquo;", "").replace("&#8217;", ""); // HTML-entity apostrophes (unescaped raw response body)
        return SOFT_404_PHRASES.stream().anyMatch(normalized::contains);
    }

    private void recordDisappeared(AuctionRef ref) {
        try {
            dbManager.getJdbcTemplate().update(
                    """
                    INSERT INTO auction_events (auction_id, event_type, old_value, new_value, detected_at, spider_run_id)
                    VALUES (?, 'disappeared', ?, NULL, ?, NULL)
                    """,
                    ref.auctionId(), ref.status(), OffsetDateTime.now().toString()
            );
        } catch (Exception e) {
            // don't block the response over a failed write -- the user still
            // gets a correct alive=false answer even if the DB write fails
        }
    }

    private record CachedResult(LinkCheckResult result, Instant expiresAt) {
        boolean isExpired() {
            return Instant.now().isAfter(expiresAt);
        }
    }
}