package com.oncoord.auctionscout.notification;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.time.Duration;
import java.util.Optional;

/**
 * Records every digest email actually sent (welcome / weekly / test)
 * and doubles as the anti-spam guard: sentRecently() is the single
 * check every send path (DigestSendService) goes through before
 * actually mailing anything, so a subscriber can't be emailed twice
 * within the cooldown window regardless of which of the three paths
 * (welcome trigger, weekly scheduler, admin test-send) caused it.
 */
@Repository
public class NotificationRepository {

    private final JdbcTemplate jdbc;

    public NotificationRepository(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    /**
     * Whether ANY notification (regardless of type) went out to this
     * email within the given window. Deliberately type-agnostic — a
     * test send an hour ago should still block a weekly send from
     * firing right after it, same as the reverse.
     */
    public boolean sentRecently(String email, Duration window) {
        Optional<Long> lastSentAt = jdbc.query(
                "SELECT MAX(sent_at) AS last_sent FROM email_notifications WHERE email = ?",
                rs -> {
                    if (!rs.next()) return Optional.empty();
                    long v = rs.getLong("last_sent");
                    return rs.wasNull() ? Optional.<Long>empty() : Optional.of(v);
                },
                email
        );
        return lastSentAt.isPresent()
                && (System.currentTimeMillis() - lastSentAt.get()) < window.toMillis();
    }

    /**
     * Whether this email has ever received a notification of the given
     * type — used to gate the one-shot welcome email so it can't fire
     * twice even across multiple preference saves.
     */
    public boolean hasSentType(String email, String notificationType) {
        Integer count = jdbc.queryForObject(
                "SELECT COUNT(*) FROM email_notifications WHERE email = ? AND notification_type = ?",
                Integer.class, email, notificationType
        );
        return count != null && count > 0;
    }

    /**
     * Most recent sent_at for this email + notification type, or empty
     * if never sent. Unlike sentRecently()'s shared cross-type cooldown
     * (meant for the welcome/weekly/test trio), this is a per-type
     * "since when" cutoff -- used by SavedPropertyAlertService so a
     * weekly digest going out doesn't suppress a saved-property alert
     * due the next day, and vice versa.
     */
    public Optional<Long> findLastSentAtByType(String email, String notificationType) {
        return jdbc.query(
                "SELECT MAX(sent_at) AS last_sent FROM email_notifications WHERE email = ? AND notification_type = ?",
                rs -> {
                    if (!rs.next()) return Optional.empty();
                    long v = rs.getLong("last_sent");
                    return rs.wasNull() ? Optional.<Long>empty() : Optional.of(v);
                },
                email, notificationType
        );
    }

    /**
     * Whether this email received ANY notification (regardless of
     * type) strictly before the given point in time. Used to gate
     * whether a subscriber can be told a listing was "Removed" — if
     * nothing went out to them before the listing disappeared, they
     * never had a chance to see it in the first place, so announcing
     * its removal would be telling them about something they never
     * knew existed. See DigestService.renderChanges().
     */
    public boolean hasSentBefore(String email, long beforeEpochMillis) {
        Integer count = jdbc.queryForObject(
                "SELECT COUNT(*) FROM email_notifications WHERE email = ? AND sent_at < ?",
                Integer.class, email, beforeEpochMillis
        );
        return count != null && count > 0;
    }

    /**
     * Used by SubscriptionController.cancellationInfo() for the "you've
     * received N notifications" line on the cancellation confirmation
     * page.
     */
    public int countSentByType(String email, String notificationType) {
        Integer count = jdbc.queryForObject(
                "SELECT COUNT(*) FROM email_notifications WHERE email = ? AND notification_type = ?",
                Integer.class, email, notificationType
        );
        return count != null ? count : 0;
    }

    public void recordSent(String email, Integer subscriberId, String notificationType) {
        jdbc.update(
                "INSERT INTO email_notifications (subscriber_id, email, notification_type, sent_at) " +
                        "VALUES (?, ?, ?, ?)",
                subscriberId, email, notificationType, System.currentTimeMillis()
        );
    }
}