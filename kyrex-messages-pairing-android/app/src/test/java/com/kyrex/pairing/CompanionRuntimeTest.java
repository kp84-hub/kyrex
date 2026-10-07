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
import static org.junit.Assert.*;
import static org.robolectric.Shadows.shadowOf;

@RunWith(RobolectricTestRunner.class)
@Config(sdk = 35)
public class CompanionRuntimeTest {
    private CompanionRuntime runtime;
    private FakeLink link;
    private static class FakeLink extends CloudLink {
        Exception failure; int heartbeats;
        FakeLink() { super("https://chat.kyrex.dev", "test_token", false); }
        @Override void heartbeat(String state) throws Exception { heartbeats++; if (failure != null) throw failure; }
    }
    @Before public void create() {
        runtime = new CompanionRuntime(RuntimeEnvironment.getApplication(), new CompanionRuntime.Listener() {
            public void changed() { }
            public void event(String kind, String value) { }
        }, false);
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
}
