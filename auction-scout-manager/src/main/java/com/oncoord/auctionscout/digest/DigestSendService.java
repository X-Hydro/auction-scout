package com.oncoord.auctionscout.digest;

import com.oncoord.auctionscout.mail.DigestMailer;
import com.oncoord.auctionscout.notification.NotificationRepository;
import com.oncoord.auctionscout.subscriber.SubscriberRepository;
import org.springframework.stereotype.Service;

import java.time.Duration;
import java.time.OffsetDateTime;

/**
 * The single place all three digest-send triggers (welcome on first
 * preferences save, the weekly scheduler, and the admin test-send tool)
 * go through. Each one calls a distinct public method here rather than
 * reaching into DigestService/DigestMailer/NotificationRepository
 * directly, so the cooldown check and logging can't accidentally be
 * skipped by a future caller.
 */
@Service
public class DigestSendService {

    // "Don't send an email again within the last 4 hours" -- see
    // NotificationRepository.sentRecently(). One shared clock across
    // all three notification types by design (confirmed decision):
    // a test send an hour ago blocks a weekly send from also firing.
    private static final Duration COOLDOWN = Duration.ofHours(4);

    public static final String TYPE_WELCOME = "welcome";
    public static final String TYPE_WEEKLY = "weekly";
    public static final String TYPE_TEST = "test";

    private static final String SUBJECT = "AuctionScout Auction Watch";

    public enum SendResult { SENT, SKIPPED_COOLDOWN, ALREADY_WELCOMED }

    private final DigestService digestService;
    private final DigestMailer mailer;
    private final NotificationRepository notifications;
    private final SubscriberRepository subscribers;

    public DigestSendService(DigestService digestService,
                             DigestMailer mailer,
                             NotificationRepository notifications,
                             SubscriberRepository subscribers) {
        this.digestService = digestService;
        this.mailer = mailer;
        this.notifications = notifications;
        this.subscribers = subscribers;
    }

    /**
     * Called from PreferencesController right after a subscriber's
     * first successful states save. A no-op (ALREADY_WELCOMED) if this
     * email has ever received a welcome notification before -- makes
     * this safe to call on every save without the caller having to
     * track "is this really their first time" itself.
     */
    public SendResult sendWelcomeIfFirstTime(String email) {
        if (notifications.hasSentType(email, TYPE_WELCOME)) {
            return SendResult.ALREADY_WELCOMED;
        }
        return sendIfDue(email, TYPE_WELCOME);
    }

    /** Called once per subscriber by WeeklyDigestScheduler. */
    public SendResult sendWeekly(String email) {
        return sendIfDue(email, TYPE_WEEKLY);
    }

    /** Called by AdminTestSendController — same cooldown applies. */
    public SendResult sendTest(String email) {
        return sendIfDue(email, TYPE_TEST);
    }

    /** Iterates every active, alerts-enabled subscriber for the weekly scheduled run. */
    public void sendWeeklyToAllActiveSubscribers() {
        for (SubscriberRepository.ActiveSubscriber s : subscribers.findActiveWithAlertsEnabled()) {
            sendWeekly(s.email());
        }
    }

    private SendResult sendIfDue(String email, String notificationType) {
        if (notifications.sentRecently(email, COOLDOWN)) {
            return SendResult.SKIPPED_COOLDOWN;
        }

        // 7-day cutoff for "what changed" -- same placeholder reasoning
        // as StatusController: real per-subscriber "last digest sent"
        // tracking isn't built, this notification log could become that
        // source later, but isn't wired up for it yet.
        String html = digestService.renderForSubscriber(email, OffsetDateTime.now().minusDays(7), true);
        mailer.send(email, SUBJECT, html);

        Integer subscriberId = subscribers.findIdByEmail(email).orElse(null);
        notifications.recordSent(email, subscriberId, notificationType);
        return SendResult.SENT;
    }
}