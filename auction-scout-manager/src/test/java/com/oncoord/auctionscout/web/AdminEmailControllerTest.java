package com.oncoord.auctionscout.web;

import com.oncoord.auctionscout.digest.DigestSendService;
import com.oncoord.auctionscout.digest.SavedPropertyAlertService;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

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
            Future<?> first = executor.submit(() -> controller.runSavedPropertyAlerts("test-admin-key", null));
            assertTrue(firstBatchStarted.await(1, TimeUnit.SECONDS));

            // This request arrives while the first batch is still in
            // progress. It must be resolved (rejected by the guard) before
            // we release the first batch -- otherwise, under contention,
            // the scheduler could delay this submission long enough for
            // the first batch to finish and release the guard first,
            // letting this one through too. Waiting on get() here proves
            // the guard was still held for this call's entire lifetime,
            // rather than merely being submitted first.
            Future<?> second = executor.submit(() -> controller.runSavedPropertyAlerts("test-admin-key", null));
            second.get(2, TimeUnit.SECONDS);

            allowBatchToFinish.countDown();
            first.get(2, TimeUnit.SECONDS);
        } finally {
            allowBatchToFinish.countDown();
            executor.shutdownNow();
        }

        // A single-run guard should make the second request reject rather
        // than let both enter the production send path.
        verify(savedPropertyAlerts, times(1)).sendToAllActiveSubscribers();
    }

    @Test
    void concurrentDryRunAndProductionRequests_shareTheSameGuard() throws Exception {
        DigestSendService digestSendService = mock(DigestSendService.class);
        SavedPropertyAlertService savedPropertyAlerts = mock(SavedPropertyAlertService.class);
        AdminEmailController controller = new AdminEmailController(
                digestSendService, savedPropertyAlerts, "test-admin-key");

        CountDownLatch previewStarted = new CountDownLatch(1);
        CountDownLatch allowPreviewToFinish = new CountDownLatch(1);
        doAnswer(invocation -> {
            previewStarted.countDown();
            assertTrue(allowPreviewToFinish.await(2, TimeUnit.SECONDS));
            return List.of();
        }).when(savedPropertyAlerts).previewAllActiveSubscribers();

        ExecutorService executor = Executors.newFixedThreadPool(2);
        try {
            Future<?> preview = executor.submit(() -> controller.runSavedPropertyAlerts(
                    "test-admin-key", new AdminEmailController.RunSavedPropertyAlertsRequest(true)));
            assertTrue(previewStarted.await(1, TimeUnit.SECONDS));

            // A real run arriving while a preview is still in progress
            // should be rejected by the same guard, not allowed to send.
            // Resolve it before releasing the preview, for the same reason
            // as the production-only test above: this proves the guard
            // was still held throughout this call, rather than relying on
            // it losing a race against the preview finishing first.
            Future<?> realRun = executor.submit(() -> controller.runSavedPropertyAlerts("test-admin-key", null));
            realRun.get(2, TimeUnit.SECONDS);

            allowPreviewToFinish.countDown();
            preview.get(2, TimeUnit.SECONDS);
        } finally {
            allowPreviewToFinish.countDown();
            executor.shutdownNow();
        }

        verify(savedPropertyAlerts, never()).sendToAllActiveSubscribers();
    }

    @Test
    @SuppressWarnings("unchecked")
    void dryRunRequest_returnsPreviewWithoutSending() {
        DigestSendService digestSendService = mock(DigestSendService.class);
        SavedPropertyAlertService savedPropertyAlerts = mock(SavedPropertyAlertService.class);
        AdminEmailController controller = new AdminEmailController(
                digestSendService, savedPropertyAlerts, "test-admin-key");

        SavedPropertyAlertService.PreviewResult preview =
                new SavedPropertyAlertService.PreviewResult(
                        "subscriber@example.com", "Updates on your saved properties", "<html>...</html>");
        when(savedPropertyAlerts.previewAllActiveSubscribers()).thenReturn(List.of(preview));

        var response = controller.runSavedPropertyAlerts(
                "test-admin-key", new AdminEmailController.RunSavedPropertyAlertsRequest(true));

        assertEquals(200, response.getStatusCode().value());
        Map<String, Object> body = (Map<String, Object>) response.getBody();
        assertEquals(Boolean.TRUE, body.get("dryRun"));
        List<Map<String, String>> results = (List<Map<String, String>>) body.get("results");
        assertEquals(1, results.size());
        assertEquals("subscriber@example.com", results.get(0).get("email"));
        assertEquals("<html>...</html>", results.get(0).get("html"));

        verify(savedPropertyAlerts, never()).sendToAllActiveSubscribers();
    }

    @Test
    void missingRequestBody_defaultsToProductionRun() {
        DigestSendService digestSendService = mock(DigestSendService.class);
        SavedPropertyAlertService savedPropertyAlerts = mock(SavedPropertyAlertService.class);
        AdminEmailController controller = new AdminEmailController(
                digestSendService, savedPropertyAlerts, "test-admin-key");
        when(savedPropertyAlerts.sendToAllActiveSubscribers()).thenReturn(0);

        controller.runSavedPropertyAlerts("test-admin-key", null);

        verify(savedPropertyAlerts, times(1)).sendToAllActiveSubscribers();
        verify(savedPropertyAlerts, never()).previewAllActiveSubscribers();
    }
}