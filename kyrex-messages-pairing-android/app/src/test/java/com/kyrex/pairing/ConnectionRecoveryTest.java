package com.kyrex.pairing;

import org.junit.Test;
import static org.junit.Assert.*;

public class ConnectionRecoveryTest {
    private ConnectionRecovery connected() {
        ConnectionRecovery recovery = new ConnectionRecovery();
        recovery.manualReconnect();
        assertTrue(recovery.beginRestore(true, false));
        recovery.verified();
        return recovery;
    }

    @Test public void returningToAppChecksConnectionWithoutReplacingHealthyDrafts() {
        ConnectionRecovery recovery = connected();
        recovery.disconnected();
        assertFalse(recovery.ready());
        assertTrue(recovery.beginProbe(true, false));
        assertFalse(recovery.beginRestore(true, false));
        recovery.verified();
        assertTrue(recovery.ready());
        assertFalse(recovery.beginRestore(true, false));
    }

    @Test public void failedProbeRestoresSavedPairingAndRequiresVerification() {
        ConnectionRecovery recovery = connected();
        recovery.disconnected();
        assertTrue(recovery.beginProbe(true, false));
        recovery.probeFailed();
        assertTrue(recovery.beginRestore(true, false));
        assertFalse(recovery.ready());
        assertFalse(recovery.beginProbe(true, false));
        assertFalse(recovery.beginRestore(true, false));
        recovery.verified();
        assertTrue(recovery.ready());
    }

    @Test public void backgroundAndBusySendCannotStartRecovery() {
        ConnectionRecovery recovery = connected();
        recovery.disconnected();
        assertFalse(recovery.beginProbe(false, false));
        assertFalse(recovery.beginProbe(true, true));
        assertTrue(recovery.beginProbe(true, false));
        recovery.probeFailed();
        assertFalse(recovery.beginRestore(false, false));
        assertFalse(recovery.beginRestore(true, true));
        assertTrue(recovery.beginRestore(true, false));
    }

    @Test public void transientFailuresBackOffAndSuccessResetsDelay() {
        ConnectionRecovery recovery = new ConnectionRecovery();
        recovery.manualReconnect();
        long[] delays = {5000, 10000, 20000, 40000, 60000, 60000};
        for (long delay : delays) {
            assertTrue(recovery.beginRestore(true, false));
            recovery.restoreFailed();
            assertEquals(delay, recovery.retryDelay());
        }
        recovery.verified();
        assertEquals(5000, recovery.retryDelay());
    }

    @Test public void revocationForgetAndShutdownBlockLateCallbacks() {
        ConnectionRecovery recovery = connected();
        recovery.disconnected();
        assertTrue(recovery.beginProbe(true, false));
        recovery.stop();
        recovery.probeFailed(); recovery.restoreFailed(); recovery.verified(); recovery.disconnected();
        assertFalse(recovery.enabled());
        assertFalse(recovery.ready());
        assertFalse(recovery.beginProbe(true, false));
        assertFalse(recovery.beginRestore(true, false));
        recovery.manualReconnect();
        assertTrue(recovery.beginRestore(true, false));
    }

    @Test public void libraryEventsDoNotStartOverlappingConnections() {
        ConnectionRecovery recovery = new ConnectionRecovery();
        recovery.manualReconnect();
        assertTrue(recovery.beginRestore(true, false));
        recovery.disconnected(); recovery.disconnected();
        assertTrue(recovery.running());
        assertFalse(recovery.beginRestore(true, false));
        assertFalse(recovery.beginProbe(true, false));
    }
}
