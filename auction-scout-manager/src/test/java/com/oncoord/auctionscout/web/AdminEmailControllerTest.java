package com.oncoord.auctionscout.web;

import com.oncoord.auctionscout.digest.DigestSendService;
import com.oncoord.auctionscout.digest.SavedPropertyAlertService;
import org.junit.jupiter.api.Test;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;

class AdminEmailControllerTest {

    @Test
    void concurrentProductionAlertRequests_startOnlyOneBatch() throws Exception {
        DigestSendService digestSendService = mock(DigestSendService.class);
        SavedPropertyAlertService savedPropertyAlerts = mock(SavedPropertyAlertService.class);
        AdminEmailController controller = new AdminEmailController(
                digestSendService, savedPropertyAlerts, "test-admin-key");

        CountDownLatch firstBatchStarted = new CountDownLatch(1);
        CountDownLatch allowBatchToFinish = new CountDownLatch(1);
        doAnswer(invocation -> {
            firstBatchStarted.countDown();
            assertTrue(allowBatchToFinish.await(2, TimeUnit.SECONDS));
            return 0;
        }).when(savedPropertyAlerts).sendToAllActiveSubscribers();

        ExecutorService executor = Executors.newFixedThreadPool(2);
        try {
            Future<?> first = executor.submit(() -> controller.runSavedPropertyAlerts("test-admin-key"));
            assertTrue(firstBatchStarted.await(1, TimeUnit.SECONDS));

            // This request arrives while the first batch is still in progress.
            Future<?> second = executor.submit(() -> controller.runSavedPropertyAlerts("test-admin-key"));
            allowBatchToFinish.countDown();

            first.get(2, TimeUnit.SECONDS);
            second.get(2, TimeUnit.SECONDS);
        } finally {
            allowBatchToFinish.countDown();
            executor.shutdownNow();
        }

        // Fails today: both requests enter the production send path. A
        // single-run guard should make the second request wait or reject it.
        verify(savedPropertyAlerts, times(1)).sendToAllActiveSubscribers();
    }
}
