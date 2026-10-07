// SPDX-License-Identifier: AGPL-3.0-or-later
package com.kyrex.pairing;

import android.os.Looper;
import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.RuntimeEnvironment;
import org.robolectric.annotation.Config;
import org.robolectric.util.ReflectionHelpers;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.CountDownLatch;
import pairbridge.Bridge;
import static org.junit.Assert.*;
import static org.robolectric.Shadows.shadowOf;

@RunWith(RobolectricTestRunner.class)
@Config(sdk = 35)
public class CompanionRuntimeTest {
    private CompanionRuntime runtime;
    private FakeLink link;
    private static class FakeRuntime extends CompanionRuntime {
        CountDownLatch snapshotStarted, releaseSnapshot;
        FakeRuntime() {
            super(RuntimeEnvironment.getApplication(), new Listener() {
                public void changed() { }
                public void event(String kind, String value) { }
            }, false);
        }
        @Override String readSnapshot(Bridge connection) throws Exception {
            if (snapshotStarted != null) {
                snapshotStarted.countDown();
                if (!releaseSnapshot.await(5, TimeUnit.SECONDS)) throw new IllegalStateException("Test snapshot timed out");
            }
            return "{}";
        }
    }
    private static class FakeLink extends CloudLink {
        Exception failure, syncFailure; int heartbeats, syncs;
        FakeLink() { super("https://chat.kyrex.dev", "test_token", false); }
        @Override void heartbeat(String state) throws Exception { heartbeats++; if (failure != null) throw failure; }
        @Override int sync(String snapshot) throws Exception { syncs++; if (syncFailure != null) throw syncFailure; return 1; }
    }
    @Before public void create() {
        runtime = new FakeRuntime();
        link = new FakeLink(); runtime.cloudLoaded = true; runtime.cloudLink = link;
        ConnectionRecovery recovery = ReflectionHelpers.getField(runtime, "recovery"); recovery.manualReconnect(); recovery.verified();
        ReflectionHelpers.setField(runtime, "background", true);
    }
    @After public void close() { runtime.close(); }
    private void checkIn() throws Exception {
        runtime.heartbeat();
        ExecutorService worker = ReflectionHelpers.getField(runtime, "presenceWorker");
        worker.submit(() -> { }).get(5, TimeUnit.SECONDS);
        shadowOf(Looper.getMainLooper()).idle();
    }
    @Test public void checkInContinuesWithoutScreenAndFailureIsVisible() throws Exception {
        checkIn(); assertEquals("Messages connected", runtime.notificationState());
        assertTrue(runtime.heartbeatState.startsWith("Last cloud check-in:"));
        link.failure = new IllegalStateException("Could not reach Kyrex.");
        checkIn(); assertEquals(2, link.heartbeats);
        assertEquals("Could not reach Kyrex.", runtime.heartbeatState);
        assertNotEquals("Messages connected", runtime.notificationState());
        assertTrue(runtime.linked()); assertFalse(link.allowSend);
    }
    @Test public void revokedLinkHaltsCheckInsUntilNewPairing() throws Exception {
        link.failure = new CloudLink.RevokedLinkException();
        checkIn(); assertFalse(runtime.linked()); assertTrue(runtime.needsUserAttention);
        assertTrue(runtime.heartbeatState.contains("revoked"));
        checkIn(); assertEquals(1, link.heartbeats);
    }
    @Test public void stoppedRuntimeIgnoresQueuedOrManualCheckIns() throws Exception {
        runtime.setActive(false, false); checkIn(); assertEquals(0, link.heartbeats);
        assertNotEquals("Messages connected", runtime.notificationState());
    }
    private void finishSync() throws Exception {
        ExecutorService worker = ReflectionHelpers.getField(runtime, "worker");
        worker.submit(() -> { }).get(5, TimeUnit.SECONDS);
        shadowOf(Looper.getMainLooper()).idle();
    }
    @Test public void periodicSnapshotsContinueAcrossSeveralIntervalsWithoutNewMessages() throws Exception {
        runtime.syncCloud(); finishSync(); assertEquals(1, link.syncs);
        for (int expected = 2; expected <= 5; expected++) {
            shadowOf(Looper.getMainLooper()).idleFor(30, TimeUnit.SECONDS);
            finishSync(); assertEquals(expected, link.syncs);
        }
        assertEquals(0, runtime.liveCount);
    }
    @Test public void failedCloudUploadRetriesWithoutOpeningScreenOrManualSync() throws Exception {
        link.syncFailure = new IllegalStateException("Network unavailable");
        runtime.syncCloud(); finishSync(); assertEquals(1, link.syncs);
        link.syncFailure = null;
        shadowOf(Looper.getMainLooper()).idleFor(30, TimeUnit.SECONDS);
        finishSync(); assertEquals(2, link.syncs);
        assertTrue(runtime.cloudState.startsWith("Synced 1 text messages"));
    }
    @Test public void busySendDefersSyncRatherThanPermanentlyDroppingIt() throws Exception {
        ReflectionHelpers.setField(runtime, "sendBusy", true);
        runtime.syncCloud(); finishSync(); assertEquals(0, link.syncs);
        shadowOf(Looper.getMainLooper()).idleFor(30, TimeUnit.SECONDS);
        assertEquals(0, link.syncs);
        ReflectionHelpers.setField(runtime, "sendBusy", false);
        shadowOf(Looper.getMainLooper()).idleFor(5, TimeUnit.SECONDS);
        finishSync(); assertEquals(1, link.syncs);
    }
    @Test public void stopAndRevocationDoNotContinuePeriodicUploads() throws Exception {
        runtime.syncCloud(); finishSync(); runtime.setActive(false, false);
        shadowOf(Looper.getMainLooper()).idleFor(60, TimeUnit.SECONDS);
        finishSync(); assertEquals(1, link.syncs);
        ReflectionHelpers.setField(runtime, "background", true);
        link.syncFailure = new CloudLink.RevokedLinkException();
        runtime.syncCloud(); finishSync(); assertEquals(2, link.syncs);
        shadowOf(Looper.getMainLooper()).idleFor(60, TimeUnit.SECONDS);
        finishSync(); assertEquals(2, link.syncs); assertFalse(runtime.linked());
    }
    @Test public void heartbeatTimerRunsForMoreThanSixMinutesWithoutScreen() throws Exception {
        checkIn();
        for (int i = 0; i < 38; i++) {
            shadowOf(Looper.getMainLooper()).idleFor(10, TimeUnit.SECONDS);
            ExecutorService presenceWorker = ReflectionHelpers.getField(runtime, "presenceWorker");
            presenceWorker.submit(() -> { }).get(5, TimeUnit.SECONDS);
            shadowOf(Looper.getMainLooper()).idle();
        }
        assertEquals(39, link.heartbeats);
        assertEquals("Messages connected", runtime.notificationState());
    }
    @Test public void slowSnapshotDoesNotBlockIndependentHeartbeat() throws Exception {
        FakeRuntime session = (FakeRuntime) runtime;
        session.snapshotStarted = new CountDownLatch(1); session.releaseSnapshot = new CountDownLatch(1);
        try {
            runtime.syncCloud(); assertTrue(session.snapshotStarted.await(5, TimeUnit.SECONDS));
            checkIn(); assertEquals(1, link.heartbeats); assertEquals(0, link.syncs);
            assertEquals("Messages connected", runtime.notificationState());
        } finally { session.releaseSnapshot.countDown(); }
        finishSync(); assertEquals(1, link.syncs);
    }
}
