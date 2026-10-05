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

internal class SyncHttpException(val statusCode: Int) : Exception("Server rejected sync")

internal class HealthSync(private val context: Context) {
    val prefs = context.getSharedPreferences("pairing", Context.MODE_PRIVATE)
    companion object {
        private val syncMutex = Mutex()
        val permissions = setOf(
            HealthPermission.getReadPermission(StepsRecord::class),
            HealthPermission.getReadPermission(ExerciseSessionRecord::class),
            HealthPermission.getReadPermission(HeartRateRecord::class),
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
            if (conn.responseCode !in 200..299) throw SyncHttpException(conn.responseCode)
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

    suspend fun sync(background: Boolean = false): Int = syncMutex.withLock {
        check(HealthConnectClient.getSdkStatus(context) == HealthConnectClient.SDK_AVAILABLE)
        val client = HealthConnectClient.getOrCreate(context)
        val allowed = client.permissionController.getGrantedPermissions()
        check(allowed.intersect(permissions).isNotEmpty())
        if (background) {
            check(backgroundAvailable(client))
            if (HealthPermission.PERMISSION_READ_HEALTH_DATA_IN_BACKGROUND !in allowed) throw SecurityException("Background permission removed")
            check(prefs.getBoolean("auto_sync", false))
        }
        val base = checkedServer(prefs.getString("server", "") ?: "")
        val deviceToken = token()
        val end = Instant.now(); val filter = TimeRangeFilter.between(end.minus(Duration.ofDays(7)), end)
        val batch = mutableListOf<JSONObject>(); var sent = 0
        suspend fun send(body: JSONObject) {
            currentCoroutineContext().ensureActive()
            if (background) check(prefs.getBoolean("auto_sync", false))
            check(prefs.getString("server", "") == base && token() == deviceToken)
            post(base, "/api/connections/samsung_health/sync", body, deviceToken)
        }
        suspend fun flush() {
            if (batch.isEmpty()) return
            send(JSONObject().put("records", JSONArray(batch)))
            sent += batch.size; batch.clear()
        }
        suspend fun emit(r: Record, kind: String, start: Instant, finish: Instant, field: String, value: Number, suffix: String = "") {
            if (r.metadata.dataOrigin.packageName != "com.sec.android.app.shealth") return
            batch.add(JSONObject().put("id", r.metadata.id + suffix).put("origin", r.metadata.dataOrigin.packageName)
                .put("type", kind).put("start", start.toString()).put("end", finish.toString()).put(field, value))
            if (batch.size >= 400) flush()
        }
        if (HealthPermission.getReadPermission(StepsRecord::class) in allowed) {
            var page: String? = null
            do {
                val result = client.readRecords(ReadRecordsRequest(StepsRecord::class, filter, pageToken = page))
                for (r in result.records) emit(r, "steps", r.startTime, r.endTime, "count", r.count)
                page = result.pageToken
            } while (!page.isNullOrEmpty())
        }
        if (HealthPermission.getReadPermission(ExerciseSessionRecord::class) in allowed) {
            var page: String? = null
            do {
                val result = client.readRecords(ReadRecordsRequest(ExerciseSessionRecord::class, filter, pageToken = page))
                for (r in result.records) emit(r, "workout", r.startTime, r.endTime, "exercise_type", r.exerciseType)
                page = result.pageToken
            } while (!page.isNullOrEmpty())
        }
        if (HealthPermission.getReadPermission(SleepSessionRecord::class) in allowed) {
            var page: String? = null
            do {
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
                val result = client.readRecords(ReadRecordsRequest(HeartRateRecord::class, filter, pageToken = page))
                for (r in result.records) for (sample in r.samples) emit(r, "heart_rate", sample.time, sample.time, "bpm", sample.beatsPerMinute, ":${sample.time}")
                page = result.pageToken
            } while (!page.isNullOrEmpty())
        }
        flush()
        // Empty sync still records the successful contact time.
        send(JSONObject().put("records", JSONArray()).put("complete", true))
        prefs.edit().putLong("last_sync", System.currentTimeMillis()).putInt("last_count", sent).putString("sync_status", "Sync completed").apply()
        sent
    }
}
