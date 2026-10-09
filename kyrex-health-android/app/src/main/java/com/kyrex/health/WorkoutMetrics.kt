package com.kyrex.health

import androidx.health.connect.client.aggregate.AggregateMetric
import androidx.health.connect.client.aggregate.AggregationResult
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.*
import androidx.health.connect.client.records.metadata.DataOrigin
import androidx.health.connect.client.request.AggregateRequest
import androidx.health.connect.client.time.TimeRangeFilter
import kotlinx.coroutines.CancellationException
import org.json.JSONObject

internal const val SAMSUNG_ORIGIN = "com.sec.android.app.shealth"

internal fun exerciseLabel(type: Int): String = when (type) {
    ExerciseSessionRecord.EXERCISE_TYPE_OTHER_WORKOUT -> "Other workout"
    ExerciseSessionRecord.EXERCISE_TYPE_HIGH_INTENSITY_INTERVAL_TRAINING -> "HIIT"
    ExerciseSessionRecord.EXERCISE_TYPE_STRENGTH_TRAINING -> "Strength training"
    ExerciseSessionRecord.EXERCISE_TYPE_WEIGHTLIFTING -> "Weightlifting"
    ExerciseSessionRecord.EXERCISE_TYPE_RUNNING -> "Running"
    ExerciseSessionRecord.EXERCISE_TYPE_RUNNING_TREADMILL -> "Treadmill running"
    ExerciseSessionRecord.EXERCISE_TYPE_WALKING -> "Walking"
    ExerciseSessionRecord.EXERCISE_TYPE_BIKING -> "Cycling"
    ExerciseSessionRecord.EXERCISE_TYPE_BIKING_STATIONARY -> "Stationary cycling"
    ExerciseSessionRecord.EXERCISE_TYPE_ELLIPTICAL -> "Elliptical"
    ExerciseSessionRecord.EXERCISE_TYPE_ROWING_MACHINE -> "Rowing machine"
    ExerciseSessionRecord.EXERCISE_TYPE_SWIMMING_POOL -> "Swimming"
    ExerciseSessionRecord.EXERCISE_TYPE_YOGA -> "Yoga"
    else -> "Workout"
}

internal fun workoutAggregateRequest(session: ExerciseSessionRecord, allowed: Set<String>): AggregateRequest? {
    val metrics = mutableSetOf<AggregateMetric<*>>()
    if (HealthPermission.getReadPermission(HeartRateRecord::class) in allowed)
        metrics.addAll(setOf(HeartRateRecord.BPM_AVG, HeartRateRecord.BPM_MIN,
            HeartRateRecord.BPM_MAX, HeartRateRecord.MEASUREMENTS_COUNT))
    if (HealthPermission.getReadPermission(ActiveCaloriesBurnedRecord::class) in allowed)
        metrics.add(ActiveCaloriesBurnedRecord.ACTIVE_CALORIES_TOTAL)
    if (HealthPermission.getReadPermission(TotalCaloriesBurnedRecord::class) in allowed)
        metrics.add(TotalCaloriesBurnedRecord.ENERGY_TOTAL)
    if (HealthPermission.getReadPermission(DistanceRecord::class) in allowed)
        metrics.add(DistanceRecord.DISTANCE_TOTAL)
    if (HealthPermission.getReadPermission(StepsRecord::class) in allowed)
        metrics.add(StepsRecord.COUNT_TOTAL)
    if (metrics.isEmpty()) return null
    return AggregateRequest(metrics = metrics,
        timeRangeFilter = TimeRangeFilter.between(session.startTime, session.endTime),
        dataOriginFilter = setOf(DataOrigin(SAMSUNG_ORIGIN)))
}

internal suspend fun workoutExtras(session: ExerciseSessionRecord, allowed: Set<String>,
                                  aggregate: suspend (AggregateRequest) -> AggregationResult): JSONObject {
    val output = JSONObject().put("exercise_label", exerciseLabel(session.exerciseType))
    session.title?.trim()?.filter { it.code >= 32 }?.take(120)?.takeIf { it.isNotBlank() }?.let { output.put("title", it) }
    val metrics = JSONObject()
    val statuses = JSONObject()
    val groups = mapOf("heart_rate" to HeartRateRecord::class,
        "active_calories" to ActiveCaloriesBurnedRecord::class,
        "total_calories" to TotalCaloriesBurnedRecord::class,
        "distance" to DistanceRecord::class, "steps" to StepsRecord::class)
    groups.forEach { (name, type) -> statuses.put(name,
        if (HealthPermission.getReadPermission(type) in allowed) "no_data" else "permission_missing") }
    // Isolate optional reads: a denied/unsupported distance read must not
    // discard the session's available heart rate or calories.
    for ((group, type) in groups) {
        val permission = HealthPermission.getReadPermission(type)
        if (permission !in allowed) continue
        val request = workoutAggregateRequest(session, setOf(permission)) ?: continue
        try {
            val values = aggregate(request)
            fun metric(key: String, value: Number?, group: String) {
                if (value != null && value.toDouble().isFinite()) {
                    metrics.put(key, value); statuses.put(group, "ok")
                }
            }
            metric("heart_rate_avg_bpm", values[HeartRateRecord.BPM_AVG], "heart_rate")
            metric("heart_rate_min_bpm", values[HeartRateRecord.BPM_MIN], "heart_rate")
            metric("heart_rate_max_bpm", values[HeartRateRecord.BPM_MAX], "heart_rate")
            metric("heart_rate_sample_count", values[HeartRateRecord.MEASUREMENTS_COUNT], "heart_rate")
            metric("active_calories_kcal", values[ActiveCaloriesBurnedRecord.ACTIVE_CALORIES_TOTAL]?.inKilocalories, "active_calories")
            metric("total_calories_kcal", values[TotalCaloriesBurnedRecord.ENERGY_TOTAL]?.inKilocalories, "total_calories")
            metric("distance_meters", values[DistanceRecord.DISTANCE_TOTAL]?.inMeters, "distance")
            metric("steps", values[StepsRecord.COUNT_TOTAL], "steps")
        } catch (e: CancellationException) { throw e }
        catch (_: Exception) {
            // Keep the measured session when optional details fail. Never
            // replace missing data with zero or log health/provider details.
            statuses.put(group, "read_failed")
        }
    }
    return output.put("session_metrics", metrics).put("metric_status", statuses)
}
