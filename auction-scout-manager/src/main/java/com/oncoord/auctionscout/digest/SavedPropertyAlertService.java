package com.oncoord.auctionscout.digest;

import com.oncoord.auctionscout.mail.DigestMailer;
import com.oncoord.auctionscout.notification.NotificationRepository;
import com.oncoord.auctionscout.saved.SavedPropertiesRepository;
import com.oncoord.auctionscout.subscriber.SubscriberRepository;
import org.springframework.stereotype.Service;

import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;

/**
 * Alert for saved/tracked properties -- date changes and removals only
 * (see DigestService.renderSavedPropertyAlert()), for whatever
 * properties each actively-subscribed subscriber has saved. Triggered
 * manually (see AdminTestSendController.runSavedPropertyAlerts()),
 * after confirming the new weekly dataset has actually finished
 * loading -- deliberately not on a fixed cron like WeeklyDigestScheduler,
 * since "assume it's done by 9am Monday" can be silently wrong if the
 * load runs late.
 *
 * Deliberately separate from DigestSendService rather than a third
 * method on it: that class's cooldown (NotificationRepository
 * .sentRecently()) is a shared, cross-type gate meant for the
 * welcome/weekly/test trio -- sharing it here could wrongly suppress
 * this alert if it happened to fire within that cooldown window of one
 * of those. This uses its own per-type "last sent" cutoff instead (see
 * NotificationRepository.findLastSentAtByType()).
 */
@Service
public class SavedPropertyAlertService {

    public static final String TYPE_SAVED_PROPERTY_ALERT = "saved_property_alert";

    // Used only for a subscriber's very first alert (no prior "last
    // sent" row to anchor to). The job now runs weekly (see
    // WeeklyDigestScheduler), matching DigestSendService's own 7-day
    // placeholder cutoff -- a small buffer past exactly 7 days in case
    // this run lands slightly later than the previous one.
    private static final Duration FALLBACK_LOOKBACK = Duration.ofDays(8);

    private final DigestService digestService;
    private final DigestMailer mailer;
    private final NotificationRepository notifications;
    private final SubscriberRepository subscribers;
    private final SavedPropertiesRepository savedProperties;

    public SavedPropertyAlertService(DigestService digestService,
                                     DigestMailer mailer,
                                     NotificationRepository notifications,
                                     SubscriberRepository subscribers,
                                     SavedPropertiesRepository savedProperties) {
        this.digestService = digestService;
        this.mailer = mailer;
        this.notifications = notifications;
        this.subscribers = subscribers;
        this.savedProperties = savedProperties;
    }

    /**
     * Called manually via POST /admin/run-saved-property-alerts, after
     * confirming the week's data load has finished. Reuses
     * findActiveWithAlertsEnabled() -- the same active-and-alerts-
     * enabled subscriber set the weekly digest uses -- so this respects
     * the same email_alerts_enabled preference rather than introducing
     * a second opt-in.
     *
     * @return how many subscribers actually got an email -- not the
     *         total number checked. Most eligible subscribers on any
     *         given run will have nothing to report and are silently
     *         skipped (see sendIfChanged()), so the admin page needs
     *         this count to tell "ran and found nothing" apart from
     *         "ran and it actually did something."
     */
    public int sendToAllActiveSubscribers() {
        int sentCount = 0;
        for (SubscriberRepository.ActiveSubscriber s : subscribers.findActiveWithAlertsEnabled()) {
            if (sendIfChanged(s.email())) {
                sentCount++;
            }
        }
        return sentCount;
    }

    // Wide net for the admin test tool only -- real production sends use
    // findLastSentAtByType()'s tight per-subscriber cutoff instead. This
    // just maximizes the odds of finding *something* real to render on
    // whatever test address is used, purely for checking deliverability
    // and rendering (matches admin-test-send.html's own stated purpose).
    private static final Duration TEST_LOOKBACK = Duration.ofDays(90);

    public enum TestResult { SENT, NO_SAVED_PROPERTIES }

    /**
     * Admin test-send counterpart to sendIfChanged(). Deliberately does
     * NOT mirror DigestSendService.sendTest()'s "go through the real
     * cooldown, no bypass" rule -- there's no cooldown/anti-spam gate
     * on this path to begin with (see class javadoc), so there's
     * nothing equivalent to preserve. What this DOES have to protect:
     * never calls notifications.recordSent() -- a test send must not
     * advance a real subscriber's per-type "last sent" cutoff, or the
     * next genuine weekly run could silently skip real changes that
     * fell inside the gap this test's wider lookback covered.
     */
    public TestResult sendTestAlert(String email) {
        List<Long> propertyIds = savedProperties.findByEmail(email).stream()
                .map(SavedPropertiesRepository.SavedProperty::propertyId)
                .toList();
        if (propertyIds.isEmpty()) {
            return TestResult.NO_SAVED_PROPERTIES;
        }

        OffsetDateTime since = OffsetDateTime.now().minus(TEST_LOOKBACK);
        String html = digestService.renderSavedPropertyAlertForTest(email, propertyIds, since);
        mailer.send(email, "Updates on your saved properties (TEST)", html);
        return TestResult.SENT;
    }

    /** @return true if an email was actually sent, false if there was nothing to report. */
    private boolean sendIfChanged(String email) {
        List<Long> propertyIds = savedProperties.findByEmail(email).stream()
                .map(SavedPropertiesRepository.SavedProperty::propertyId)
                .toList();
        if (propertyIds.isEmpty()) {
            return false;
        }

        OffsetDateTime since = notifications.findLastSentAtByType(email, TYPE_SAVED_PROPERTY_ALERT)
                .map(epochMillis -> Instant.ofEpochMilli(epochMillis).atOffset(ZoneOffset.UTC))
                .orElse(OffsetDateTime.now().minus(FALLBACK_LOOKBACK));

        String html = digestService.renderSavedPropertyAlert(email, propertyIds, since);
        if (html == null) {
            // Nothing to report -- no email sent, and deliberately no
            // notification row recorded either, so the next run still
            // looks back to this same "since" cutoff rather than losing
            // track of how far it's already checked.
            return false;
        }

        mailer.send(email, "Updates on your saved properties", html);
        Integer subscriberId = subscribers.findIdByEmail(email).orElse(null);
        notifications.recordSent(email, subscriberId, TYPE_SAVED_PROPERTY_ALERT);
        return true;
    }
}