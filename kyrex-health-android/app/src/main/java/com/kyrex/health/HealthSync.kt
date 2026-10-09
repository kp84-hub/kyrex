package com.kyrex.health

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.*
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.time.TimeRangeFilter
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.net.URI
import java.net.URL
import java.security.KeyStore
import java.time.Duration
import java.time.Instant
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import javax.net.ssl.HttpsURLConnection

import androidx.health.connect.client.HealthConnectFeatures
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

internal class SyncHttpException(val statusCode: Int, val safeDetail: String? = null) : Exception("Server rejected sync")
// Keep valid measurements when a provider returns an anomalous interval. Never rewrite its dates.
internal fun supportedHealthTimestamp(start: Instant, finish: Instant, now: Instant): Boolean =
    !finish.isBefore(start) && Duration.between(start, finish) <= Duration.ofDays(2) &&
        !start.isBefore(now.minus(Duration.ofDays(91))) && !finish.isAfter(now.plusSeconds(300))

internal class HealthSetupException(message: String) : Exception(message)

internal fun safeServerDetail(raw: String): String? {
    val allowed = setOf(
        "Health companion pairing was revoked. Pair again.", "Pair the health companion first.",
        "Health record timestamps are outside the supported range.", "Health measurement is invalid.",
        "Health record needs a valid id and origin.", "Invalid health record.",
        "Unsupported health record type.", "Upload at most 500 health records per batch.")
    return try { JSONObject(raw).optString("detail").takeIf { it in allowed } } catch (_: Exception) { null }
}

internal fun healthFailureMessage(action: String, error: Exception): String {
    val reason = when (error) {
        is HealthSetupException -> error.message ?: "Pair and allow health access first."
        is SyncHttpException -> (error.safeDetail ?: when (error.statusCode) {
            400 -> "The server rejected the request. If pairing, generate a fresh code."
            401, 403 -> "Pairing is no longer valid. Pair with Kyrex again."
            404 -> "The health endpoint is missing. Check the server address and deployment."
            413 -> "The upload batch is too large."
            429 -> "The server is busy. Wait a minute and retry."
            in 500..599 -> "The server failed to process the request."
            else -> "The server rejected the request."
        }) + " (HTTP ${error.statusCode})"
        is java.net.SocketTimeoutException -> "The server timed out. Check your connection and retry."
        is java.net.UnknownHostException -> "Cannot find the server. Check the address and internet connection."
        is javax.net.ssl.SSLException -> "Could not establish a secure connection to the server."
        is SecurityException -> "Health access was denied. Tap Allow Health Connect access."
        is java.security.GeneralSecurityException -> "Saved pairing could not be opened. Pair with Kyrex again."
        is java.io.IOException -> "Connection or Health Connect read failed (${error.javaClass.simpleName})."
        else -> "Unexpected ${error.javaClass.simpleName}."
    }
    return "$action failed: $reason"
}

internal class HealthSync(private val context: Context) {
    val prefs = context.getSharedPreferences("pairing", Context.MODE_PRIVATE)
    companion object {
        private val syncMutex = Mutex()
        val permissions = setOf(
            HealthPermission.getReadPermission(StepsRecord::class),
            HealthPermission.getReadPermission(ExerciseSessionRecord::class),
            HealthPermission.getReadPermission(HeartRateRecord::class),
            HealthPermission.getReadPermission(ActiveCaloriesBurnedRecord::class),
            HealthPermission.getReadPermission(TotalCaloriesBurnedRecord::class),
            HealthPermission.getReadPermission(DistanceRecord::class),
            HealthPermission.getReadPermission(SleepSessionRecord::class))
        fun backgroundAvailable(client: HealthConnectClient) = client.features.getFeatureStatus(
            HealthConnectFeatures.FEATURE_READ_HEALTH_DATA_IN_BACKGROUND) == HealthConnectFeatures.FEATURE_STATUS_AVAILABLE
    }
    fun checkedServer(raw: String): String {
        val u = URI(raw.trim().trimEnd('/'))
        require(u.scheme == "https" && !u.host.isNullOrBlank() && u.userInfo == null && u.query == null && u.fragment == null && (u.path.isNullOrEmpty() || u.path == "/"))
        return u.toString().trimEnd('/')
    }

    suspend fun post(base: String, path: String, body: JSONObject, token: String? = null): JSONObject = withContext(Dispatchers.IO) {
        val conn = URL(base + path).openConnection() as HttpsURLConnection
        try {
            conn.instanceFollowRedirects = false; conn.requestMethod = "POST"; conn.doOutput = true
            conn.connectTimeout = 20000; conn.readTimeout = 30000
            conn.setRequestProperty("Content-Type", "application/json")
            if (token != null) conn.setRequestProperty("Authorization", "Bearer $token")
            conn.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            if (conn.responseCode !in 200..299) {
                // Never display arbitrary response bodies: only known, non-sensitive validation reasons.
                val detail = try { conn.errorStream?.use { input ->
                    val buffer = ByteArray(4096); val count = input.read(buffer)
                    if (count > 0) safeServerDetail(String(buffer, 0, count, Charsets.UTF_8)) else null
                } } catch (_: Exception) { null }
                throw SyncHttpException(conn.responseCode, detail)
            }
            val raw = conn.inputStream.use { input ->
                val out = java.io.ByteArrayOutputStream()
                val buffer = ByteArray(8192)
                while (true) { val count = input.read(buffer); if (count < 0) break; check(out.size() + count <= 256000); out.write(buffer, 0, count) }
                out.toByteArray()
            }
            JSONObject(String(raw, Charsets.UTF_8))
        } finally { conn.disconnect() }
    }

    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        if (!store.containsAlias("kyrex-health")) KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").apply {
            init(KeyGenParameterSpec.Builder("kyrex-health", KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build())
            generateKey()
        }
        return store.getKey("kyrex-health", null) as SecretKey
    }
    fun seal(raw: String): String {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding"); cipher.init(Cipher.ENCRYPT_MODE, key())
        return Base64.encodeToString(cipher.iv + cipher.doFinal(raw.toByteArray()), Base64.NO_WRAP)
    }
    private fun token(): String {
        val data = Base64.decode(prefs.getString("token", null) ?: error("Pair first"), Base64.NO_WRAP)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding"); cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, data.copyOfRange(0, 12)))
        return String(cipher.doFinal(data.copyOfRange(12, data.size)), Charsets.UTF_8)
    }

    suspend fun sync(background: Boolean = false, progress: (String) -> Unit = {}): Int = syncMutex.withLock {
        if (HealthConnectClient.getSdkStatus(context) != HealthConnectClient.SDK_AVAILABLE) throw HealthSetupException("Health Connect is unavailable. Install or update it first.")
        val client = HealthConnectClient.getOrCreate(context)
        val allowed = client.permissionController.getGrantedPermissions()
        if (allowed.intersect(permissions).isEmpty()) throw HealthSetupException("No health read permissions. Tap Allow Health Connect access.")
        if (background) {
            check(backgroundAvailable(client))
            if (HealthPermission.PERMISSION_READ_HEALTH_DATA_IN_BACKGROUND !in allowed) throw SecurityException("Background permission removed")
            check(prefs.getBoolean("auto_sync", false))
        }
        if (!prefs.contains("token") || !prefs.contains("server")) throw HealthSetupException("This installation is not paired. Enter a fresh code and tap Pair with Kyrex.")
        val base = checkedServer(prefs.getString("server", "") ?: "")
        val deviceToken = token()
        val end = Instant.now(); val filter = TimeRangeFilter.between(end.minus(Duration.ofDays(7)), end)
        val batch = mutableListOf<JSONObject>(); var sent = 0; var skipped = 0
        suspend fun send(body: JSONObject) {
            currentCoroutineContext().ensureActive()
            if (background) check(prefs.getBoolean("auto_sync", false))
            check(prefs.getString("server", "") == base && token() == deviceToken)
            post(base, "/api/connections/samsung_health/sync", body, deviceToken)
        }
        suspend fun flush() {
            if (batch.isEmpty()) return
            progress("Uploading batch after $sent readings")
            send(JSONObject().put("records", JSONArray(batch)))
            sent += batch.size; batch.clear()
        }
        suspend fun emit(r: Record, kind: String, start: Instant, finish: Instant, field: String, value: Number, suffix: String = "", extras: JSONObject? = null) {
            if (r.metadata.dataOrigin.packageName != "com.sec.android.app.shealth") return
            if (!supportedHealthTimestamp(start, finish, Instant.now())) { skipped++; return }
            val row = JSONObject().put("id", r.metadata.id + suffix).put("origin", r.metadata.dataOrigin.packageName)
                .put("type", kind).put("start", start.toString()).put("end", finish.toString()).put(field, value)
            extras?.keys()?.forEach { name -> row.put(name, extras.get(name)) }
            batch.add(row)
            if (batch.size >= 400) flush()
        }
        if (HealthPermission.getReadPermission(StepsRecord::class) in allowed) {
            var page: String? = null
            do {
                progress("Reading Samsung Health steps")
                val result = client.readRecords(ReadRecordsRequest(StepsRecord::class, filter, pageToken = page))
                for (r in result.records) emit(r, "steps", r.startTime, r.endTime, "count", r.count)
                page = result.pageToken
            } while (!page.isNullOrEmpty())
        }
        if (HealthPermission.getReadPermission(ExerciseSessionRecord::class) in allowed) {
            var page: String? = null
            do {
                progress("Reading Samsung Health workouts")
                val result = client.readRecords(ReadRecordsRequest(ExerciseSessionRecord::class, filter, pageToken = page))
                for (r in result.records) {
                    if (r.metadata.dataOrigin.packageName != SAMSUNG_ORIGIN) continue
                    if (!supportedHealthTimestamp(r.startTime, r.endTime, Instant.now())) { skipped++; continue }
                    val details = workoutExtras(r, allowed, client::aggregate)
                    emit(r, "workout", r.startTime, r.endTime, "exercise_type", r.exerciseType, extras = details)
                }
                page = result.pageToken
            } while (!page.isNullOrEmpty())
        }
        if (HealthPermission.getReadPermission(SleepSessionRecord::class) in allowed) {
            var page: String? = null
            do {
                progress("Reading Samsung Health sleep")
                val result = client.readRecords(ReadRecordsRequest(SleepSessionRecord::class, filter, pageToken = page))
                for (r in result.records) {
                    val sleepStages = setOf(SleepSessionRecord.STAGE_TYPE_SLEEPING, SleepSessionRecord.STAGE_TYPE_LIGHT, SleepSessionRecord.STAGE_TYPE_DEEP, SleepSessionRecord.STAGE_TYPE_REM)
                    val duration = r.stages.filter { it.stage in sleepStages }.sumOf { Duration.between(it.startTime, it.endTime).seconds }
                    // No staging means session duration is unavailable as actual sleep time; skip rather than invent.
                    if (duration > 0) emit(r, "sleep", r.startTime, r.endTime, "duration_seconds", duration)
                }
                page = result.pageToken
            } while (!page.isNullOrEmpty())
        }
        if (HealthPermission.getReadPermission(HeartRateRecord::class) in allowed) {
            var page: String? = null
            do {
                progress("Reading Samsung Health heart rate")
                val result = client.readRecords(ReadRecordsRequest(HeartRateRecord::class, filter, pageToken = page))
                for (r in result.records) for (sample in r.samples) emit(r, "heart_rate", sample.time, sample.time, "bpm", sample.beatsPerMinute, ":${sample.time}")
                page = result.pageToken
            } while (!page.isNullOrEmpty())
        }
        flush()
        // Empty sync still records the successful contact time.
        progress("Completing upload of $sent readings")
        send(JSONObject().put("records", JSONArray()).put("complete", true).put("skipped_records", skipped))
        prefs.edit().putLong("last_sync", System.currentTimeMillis()).putInt("last_count", sent).putInt("last_skipped", skipped).putString("sync_status", if (skipped > 0) "Sync completed with $skipped readings skipped due to invalid timestamps. Data coverage is incomplete." else "Sync completed").apply()
        sent
    }
}
