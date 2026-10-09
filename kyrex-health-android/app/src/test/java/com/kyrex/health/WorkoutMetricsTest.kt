package com.kyrex.health

import androidx.health.connect.client.aggregate.AggregationResult
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.*
import androidx.health.connect.client.records.metadata.DataOrigin
import androidx.health.connect.client.records.metadata.Metadata
import androidx.health.connect.client.request.AggregateRequest
import androidx.health.connect.client.time.TimeRangeFilter
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.time.Instant

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
@Suppress("RestrictedApi")
class WorkoutMetricsTest {
    private val start = Instant.parse("2026-10-08T12:30:48Z")
    private val session = ExerciseSessionRecord(startTime = start, startZoneOffset = null,
        endTime = start.plusSeconds(2674), endZoneOffset = null,
        metadata = Metadata.manualEntry(), exerciseType = ExerciseSessionRecord.EXERCISE_TYPE_OTHER_WORKOUT,
        title = "  Morning circuit  ")
    private val heart = HealthPermission.getReadPermission(HeartRateRecord::class)
    private val calories = HealthPermission.getReadPermission(ActiveCaloriesBurnedRecord::class)

    @Test fun requestUsesExactSessionSamsungOriginAndOnlyGrantedMetrics() {
        val expected = AggregateRequest(metrics = setOf(HeartRateRecord.BPM_AVG,
            HeartRateRecord.BPM_MIN, HeartRateRecord.BPM_MAX, HeartRateRecord.MEASUREMENTS_COUNT),
            timeRangeFilter = TimeRangeFilter.between(start, start.plusSeconds(2674)),
            dataOriginFilter = setOf(DataOrigin(SAMSUNG_ORIGIN)))
        assertEquals(expected, workoutAggregateRequest(session, setOf(heart)))
        assertNull(workoutAggregateRequest(session, emptySet()))
    }

    @Test fun permissionsAndMissingDataDoNotBecomeZeroValues() = runBlocking {
        var reads = 0
        val result = workoutExtras(session, setOf(heart)) {
            reads++; AggregationResult(emptyMap(), emptyMap(), emptySet())
        }
        assertEquals(1, reads)
        assertEquals(0, result.getJSONObject("session_metrics").length())
        assertEquals("no_data", result.getJSONObject("metric_status").getString("heart_rate"))
        assertEquals("permission_missing", result.getJSONObject("metric_status").getString("distance"))
        assertEquals("Other workout", result.getString("exercise_label"))
        assertEquals("Morning circuit", result.getString("title"))
        workoutExtras(session, emptySet()) { error("No optional access was granted") }
        Unit
    }

    @Test fun measuredValuesAreSerializedWithExplicitUnits() = runBlocking {
        val allowed = setOf(heart, calories,
            HealthPermission.getReadPermission(TotalCaloriesBurnedRecord::class),
            HealthPermission.getReadPermission(DistanceRecord::class),
            HealthPermission.getReadPermission(StepsRecord::class))
        val values = AggregationResult(mapOf(HeartRateRecord.BPM_AVG.metricKey to 151L,
            HeartRateRecord.BPM_MIN.metricKey to 95L, HeartRateRecord.BPM_MAX.metricKey to 179L,
            HeartRateRecord.MEASUREMENTS_COUNT.metricKey to 2300L, StepsRecord.COUNT_TOTAL.metricKey to 2100L),
            mapOf(ActiveCaloriesBurnedRecord.ACTIVE_CALORIES_TOTAL.metricKey to 321.5,
                TotalCaloriesBurnedRecord.ENERGY_TOTAL.metricKey to 367.2,
                DistanceRecord.DISTANCE_TOTAL.metricKey to 1250.75), setOf(DataOrigin(SAMSUNG_ORIGIN)))
        val result = workoutExtras(session, allowed) { values }
        val metrics = result.getJSONObject("session_metrics")
        assertEquals(151, metrics.getInt("heart_rate_avg_bpm"))
        assertEquals(179, metrics.getInt("heart_rate_max_bpm"))
        assertEquals(2300, metrics.getInt("heart_rate_sample_count"))
        assertEquals(321.5, metrics.getDouble("active_calories_kcal"), 0.001)
        assertEquals(367.2, metrics.getDouble("total_calories_kcal"), 0.001)
        assertEquals(1250.75, metrics.getDouble("distance_meters"), 0.001)
        assertEquals(2100, metrics.getInt("steps"))
    }

    @Test fun failureOfOneOptionalReadPreservesOtherMeasurements() = runBlocking {
        val heartRequest = workoutAggregateRequest(session, setOf(heart))
        val result = workoutExtras(session, setOf(heart, calories)) {
            if (it == heartRequest) AggregationResult(mapOf(HeartRateRecord.BPM_AVG.metricKey to 132L),
                emptyMap(), setOf(DataOrigin(SAMSUNG_ORIGIN)))
            else throw SecurityException("private provider message")
        }
        assertEquals(132, result.getJSONObject("session_metrics").getInt("heart_rate_avg_bpm"))
        assertFalse(result.getJSONObject("session_metrics").has("active_calories_kcal"))
        assertEquals("read_failed", result.getJSONObject("metric_status").getString("active_calories"))
        assertFalse(result.toString().contains("private provider message"))
    }

    @Test fun cancelledSyncStopsOptionalReads() {
        try {
            runBlocking { workoutExtras(session, setOf(heart)) { throw CancellationException("cancelled") } }
            fail("Cancellation must propagate")
        } catch (_: CancellationException) { }
    }
}
