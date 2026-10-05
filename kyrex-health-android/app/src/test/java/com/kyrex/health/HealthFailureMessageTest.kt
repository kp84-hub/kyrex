package com.kyrex.health

import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class HealthFailureMessageTest {
    @Test fun serverBodiesCannotExposeTokensOrArbitraryDetails() {
        assertNull(safeServerDetail("{\"detail\":\"secret-device-token\"}"))
        assertNull(safeServerDetail("<html>proxy error</html>"))
        val reason = "Health companion pairing was revoked. Pair again."
        assertEquals(reason, safeServerDetail("{\"detail\":\"$reason\"}"))
        assertEquals("Uploading batch failed: $reason (HTTP 400)",
            healthFailureMessage("Uploading batch", SyncHttpException(400, reason)))
    }
    @Test fun missingPairingAndPermissionFailuresHaveDistinctRemedies() {
        assertTrue(healthFailureMessage("Sync", HealthSetupException("This installation is not paired.")).contains("not paired"))
        assertTrue(healthFailureMessage("Reading steps", SecurityException("sensitive detail")).contains("Allow Health Connect access"))
        assertFalse(healthFailureMessage("Reading steps", SecurityException("sensitive detail")).contains("sensitive detail"))
        assertTrue(healthFailureMessage("Upload", SyncHttpException(500)).contains("HTTP 500"))
    }
}
