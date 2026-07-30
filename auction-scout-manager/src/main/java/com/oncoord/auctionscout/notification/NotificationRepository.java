package com.oncoord.auctionscout.notification;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.support.GeneratedKeyHolder;
import org.springframework.jdbc.support.KeyHolder;
import org.springframework.stereotype.Repository;

import java.sql.PreparedStatement;
import java.sql.Statement;
import java.time.Duration;
import java.util.List;
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
     * Whether this specific property was actually shown to this email
     * in some past notification, strictly before the given point in
     * time. Used to gate whether a subscriber can be told a listing was
     * "Removed" — if this property never appeared in anything sent to
     * them before it disappeared, they never had a chance to see it in
     * the first place, so announcing its removal would be telling them
     * about something they never knew existed. Property-scoped (via
     * email_notification_properties) rather than "was ANY email ever
     * sent to them" -- a subscriber who's received other digests but
     * never one containing this property shouldn't pass this gate for
     * it. See DigestService.buildChangeGroups().
     */
    public boolean hasSentPropertyBefore(String email, long propertyId, long beforeEpochMillis) {
        Integer count = jdbc.queryForObject(
                "SELECT COUNT(*) FROM email_notification_properties enp " +
                        "JOIN email_notifications en ON en.notification_id = enp.notification_id " +
                        "WHERE en.email = ? AND enp.property_id = ? AND en.sent_at < ?",
                Integer.class, email, propertyId, beforeEpochMillis
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

    /**
     * @param shownPropertyIds every property actually shown in the sent
     *                         email, recorded alongside it in
     *                         email_notification_properties so a later
     *                         Removed-gate check can be property-scoped
     *                         (see hasSentPropertyBefore()). Empty for
     *                         a notification type with no property
     *                         content (e.g. welcome, though today's
     *                         callers pass it consistently either way).
     */
    public void recordSent(String email, Integer subscriberId, String notificationType, List<Long> shownPropertyIds) {
        KeyHolder keyHolder = new GeneratedKeyHolder();
        jdbc.update(connection -> {
            PreparedStatement ps = connection.prepareStatement(
                    "INSERT INTO email_notifications (subscriber_id, email, notification_type, sent_at) " +
                            "VALUES (?, ?, ?, ?)",
                    Statement.RETURN_GENERATED_KEYS);
            ps.setObject(1, subscriberId);
            ps.setString(2, email);
            ps.setString(3, notificationType);
            ps.setLong(4, System.currentTimeMillis());
            return ps;
        }, keyHolder);

        if (shownPropertyIds.isEmpty()) {
            return;
        }
        long notificationId = keyHolder.getKey().longValue();
        jdbc.batchUpdate(
                "INSERT INTO email_notification_properties (notification_id, property_id) VALUES (?, ?)",
                shownPropertyIds,
                shownPropertyIds.size(),
                (ps, propertyId) -> {
                    ps.setLong(1, notificationId);
                    ps.setLong(2, propertyId);
                });
    }
}