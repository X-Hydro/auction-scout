package com.oncoord.auctionscout.properties;

import jakarta.annotation.PreDestroy;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.SingleConnectionDataSource;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import java.io.File;
import java.util.concurrent.atomic.AtomicReference;

/**
 * A SingleConnectionDataSource opens exactly one
 * JDBC connection for its entire lifetime -- fine for a file that
 * never changes, wrong for auctionscout.db, which the Python pipeline
 * overwrites on every run. Copying a new file over the old one on
 * disk does nothing to a connection that's already open; this class
 * is what actually notices the file changed and reconnects.
 *
 * Polls the file's mtime every POLL_INTERVAL_MS (see checkForUpdates).
 * On change, builds a brand-new SingleConnectionDataSource and runs it
 * through validation -- file must be non-empty, must contain a
 * 'properties' table, and that table must have at least one row --
 * before it's ever swapped in. This guards against a file caught
 * mid-write by the pipeline (briefly truncated, locked, or schema-only)
 * taking down the live connection. Consumers must call
 * getJdbcTemplate() per-query rather than holding a JdbcTemplate
 * field, or they'll keep using whatever DataSource existed at
 * injection time and never see a swap.
 *
 * Validation failure behaves differently depending on when it happens:
 * at startup it throws, so the app refuses to boot against a broken
 * config rather than silently running with no data; from the
 * scheduled poll it only logs, so a transient bad file never crashes
 * an otherwise-healthy running app.
 */
@Component
public class PropertiesDbConnectionManager {

    private static final Logger log = LoggerFactory.getLogger(PropertiesDbConnectionManager.class);

    // Must be a compile-time constant -- @Scheduled(fixedRate = ...) requires
    // one, so this can't be, say, a @Value-injected field.
    private static final long POLL_INTERVAL_MS = 60_000;

    private final String dbPath;
    private final AtomicReference<SingleConnectionDataSource> current = new AtomicReference<>();
    private volatile long lastKnownModified = -1;

    public PropertiesDbConnectionManager(
            @Value("${auctionscout.properties-db.path}") String dbPath) {
        this.dbPath = dbPath;
        reload(true); // startup must succeed -- fail fast rather than boot with no data
    }

    /**
     * Callers get a fresh JdbcTemplate per call -- cheap to construct
     * (it just wraps the current DataSource reference), and guarantees
     * every query uses whichever connection is currently live, never a
     * stale one captured before a reload.
     */
    public JdbcTemplate getJdbcTemplate() {
        return new JdbcTemplate(current.get());
    }

    @Scheduled(fixedRate = POLL_INTERVAL_MS)
    public void checkForUpdates() {
        //log.info("checkForUpdates checking for a new database file");
        File file = new File(dbPath);
        if (!file.exists()) {
            log.warn("Properties DB not found at {} -- keeping existing connection", dbPath);
            return;
        }
        long modified = file.lastModified();
        if (modified != lastKnownModified) {
            log.info("Properties DB changed (mtime {} -> {}), reloading", lastKnownModified, modified);
            // false: a live app must never crash because the Python pipeline
            // was caught mid-write copying a new file in -- log and keep
            // serving the last known-good connection instead.
            reload(false);
        }
    }

    /**
     * Releases the current SQLite connection. Primarily for tests, which
     * construct this class directly (bypassing Spring) and need to release
     * the file lock before the next test deletes/recreates the DB file --
     * @PreDestroy also lets Spring call this on real shutdown.
     */
    @PreDestroy
    public void close() {
        SingleConnectionDataSource ds = current.getAndSet(null);
        if (ds != null) {
            ds.destroy();
        }
    }

    /**
     * @param failFast true at startup (throw so the app refuses to boot
     *                 against a broken/missing DB -- better to fail loud
     *                 immediately than silently serve empty data), false
     *                 from the scheduled poll (log and keep the previous
     *                 connection instead of taking down a running app
     *                 over what's very possibly a transient bad file).
     */
    private synchronized void reload(boolean failFast) {
        File file = new File(dbPath);

        if (!file.exists()) {
            fail(failFast, "Properties DB file does not exist: " + dbPath, null);
            return;
        }
        if (file.length() == 0) {
            fail(failFast, "Properties DB file is empty (0 bytes): " + dbPath, null);
            return;
        }

        SingleConnectionDataSource newDs =
                new SingleConnectionDataSource("jdbc:sqlite:" + dbPath, true);

        try {
            // COUNT(*) FROM properties does triple duty: throws if the file
            // isn't valid SQLite, throws if the 'properties' table doesn't
            // exist, and the explicit zero-check catches a technically-valid
            // but empty database (e.g. schema applied, pipeline never ran).
            Integer count = new JdbcTemplate(newDs)
                    .queryForObject("SELECT COUNT(*) FROM properties", Integer.class);
            if (count == null) {
                throw new IllegalStateException("'properties' table does not exist");
            }
        } catch (Exception e) {
            newDs.destroy();
            fail(failFast, "New properties DB failed validation at " + dbPath
                    + " (corrupt file, missing 'properties' table, or empty table)", e);
            return;
        }

        SingleConnectionDataSource old = current.getAndSet(newDs);
        lastKnownModified = file.lastModified();
        if (old != null) {
            old.destroy();
        }
        log.info("Properties DB connection now serving {}", dbPath);
    }

    private void fail(boolean failFast, String message, Exception cause) {
        if (failFast) {
            throw new IllegalStateException(message, cause);
        }
        log.error("{} -- keeping previous connection", message, cause);
    }
}