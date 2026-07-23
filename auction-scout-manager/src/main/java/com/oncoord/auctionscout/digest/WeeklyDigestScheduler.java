package com.oncoord.auctionscout.digest;

import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/**
 * Fires the weekly digest send every Monday at 9:00 AM Eastern. Uses
 * the "America/New_York" zone name (not a fixed UTC offset) so this
 * stays correct at 9am local time across the EST/EDT transitions
 * without any code change twice a year.
 *
 * Does NOT also trigger the saved-property alert (see
 * SavedPropertyAlertService) -- that's deliberately kicked off
 * manually, via POST /admin/run-saved-property-alerts, after
 * confirming the new weekly dataset has actually finished loading,
 * rather than assuming it's done by a fixed time.
 *
 * Requires @EnableScheduling on the Spring Boot application class --
 * add it there if it isn't already present, or scheduled methods are
 * silently never invoked.
 */
@Component
public class WeeklyDigestScheduler {

    private final DigestSendService digestSendService;

    public WeeklyDigestScheduler(DigestSendService digestSendService) {
        this.digestSendService = digestSendService;
    }

    @Scheduled(cron = "0 0 9 * * MON", zone = "America/New_York")
    public void sendWeeklyDigests() {
        digestSendService.sendWeeklyToAllActiveSubscribers();
    }
}