package com.kyrex.health

import androidx.health.connect.client.records.StepsRecord
import androidx.health.connect.client.records.metadata.DataOrigin
import androidx.health.connect.client.records.metadata.Metadata
import androidx.health.connect.client.time.TimeRangeFilter
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.time.Instant

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
@Suppress("RestrictedApi")
class HealthReadsTest {
    private val instant = Instant.parse("2026-10-08T12:30:00Z")

    @Test fun samsungOriginAndTimeRangeRemainBoundOnEveryPage() {
        val filter = TimeRangeFilter.between(instant.minusSeconds(604800), instant)
        for (token in listOf(null, "next-page")) {
            val request = samsungReadRequest(StepsRecord::class, filter, token)
            assertEquals(StepsRecord::class, request.recordType)
            assertEquals(filter, request.timeRangeFilter)
            assertEquals(setOf(DataOrigin(SAMSUNG_ORIGIN)), request.dataOriginFilter)
            assertEquals(token, request.pageToken)
        }
    }

    @Test fun platformAcceptedPointIntervalCanBeDecodedWithoutChangingItsMeasurement() {
        // Android 14+ accepts equal interval endpoints. Jetpack 1.1.0 throws
        // "startTime must be before endTime" while decoding that valid record,
        // before HealthSync can examine any record in the page.
        val platform = android.health.connect.datatypes.StepsRecord.Builder(
            android.health.connect.datatypes.Metadata.Builder().build(), instant, instant, 7L,
        ).build()
        val record = StepsRecord(startTime = platform.startTime, startZoneOffset = platform.startZoneOffset,
            endTime = platform.endTime, endZoneOffset = platform.endZoneOffset,
            count = platform.count, metadata = Metadata.manualEntry())
        assertEquals(instant, record.startTime)
        assertEquals(instant, record.endTime)
        assertEquals(7L, record.count)
        assertTrue(supportedHealthTimestamp(record.startTime, record.endTime, instant))
    }

    @Test fun platformStillRejectsReversedIntervals() {
        try {
            StepsRecord(startTime = instant, startZoneOffset = null,
                endTime = instant.minusSeconds(1), endZoneOffset = null,
                count = 7, metadata = Metadata.manualEntry())
            fail("Invalid intervals must not be repaired or accepted")
        } catch (_: IllegalArgumentException) { }
    }
}
