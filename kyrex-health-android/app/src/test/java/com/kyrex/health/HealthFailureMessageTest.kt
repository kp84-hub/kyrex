package com.kyrex.health

import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class HealthFailureMessageTest {
    @Test fun anomalousIntervalsAreExcludedWithoutChangingValidReadings() {
        val now = java.time.Instant.parse("2026-10-05T15:00:00Z")
        val intervals = listOf(
            now.minusSeconds(3600) to now.minusSeconds(60),
            now.minusSeconds(60) to now.plusSeconds(301),
            now.minusSeconds(172801) to now,
            now.minusSeconds(91L * 86400 + 1) to now.minusSeconds(91L * 86400),
            now to now.minusSeconds(1),
            now to now)
        assertEquals(listOf(intervals[0], intervals[5]), intervals.filter { supportedHealthTimestamp(it.first, it.second, now) })
        assertTrue(supportedHealthTimestamp(now.minusSeconds(60), now.plusSeconds(300), now))
    }
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
