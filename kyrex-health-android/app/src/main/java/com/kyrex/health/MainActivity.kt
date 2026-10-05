package com.kyrex.health

import android.os.Bundle
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import android.widget.*
import androidx.activity.ComponentActivity
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.PermissionController
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.*
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.time.TimeRangeFilter
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
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

private const val PRIVACY = "Kyrex Health reads only Samsung Health workouts, steps, heart rate and sleep you allow through Health Connect. Tap Sync to send the last 7 days to your paired Kyrex server. Records are encrypted on that server and pruned to a 90-day window during sync. This app never writes health data or uploads in the background. Disconnect Samsung Health in Kyrex to revoke this phone and erase retained records. Remove permissions in Android settings to stop local access. Other Health Connect sources are excluded."

class PrivacyActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(TextView(this).apply { text = PRIVACY; setPadding(32, 48, 32, 32) })
    }
}

class MainActivity : ComponentActivity() {
    private val prefs by lazy { getSharedPreferences("pairing", MODE_PRIVATE) }
    private lateinit var server: EditText
    private lateinit var code: EditText
    private lateinit var status: TextView
    private val permissions = setOf(
        HealthPermission.getReadPermission(StepsRecord::class),
        HealthPermission.getReadPermission(ExerciseSessionRecord::class),
        HealthPermission.getReadPermission(HeartRateRecord::class),
        HealthPermission.getReadPermission(SleepSessionRecord::class))
    private val permissionRequest = registerForActivityResult(PermissionController.createRequestPermissionResultContract()) { granted ->
        status.text = "Granted ${granted.size} data permissions. Tap Sync when ready."
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val layout = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(32, 32, 32, 32) }
        layout.addView(TextView(this).apply { text = "Kyrex Health"; textSize = 24f })
        layout.addView(TextView(this).apply { text = PRIVACY })
        server = EditText(this).apply { hint = "Kyrex server, https://…"; setText(prefs.getString("server", "")); inputType = android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_VARIATION_URI }
        code = EditText(this).apply { hint = "Pairing code from Kyrex Connections" }
        status = TextView(this)
        layout.addView(server); layout.addView(code)
        fun button(label: String, action: () -> Unit) { layout.addView(Button(this).apply { text = label; setOnClickListener { action() } }) }
        button("Pair with Kyrex") { runTask {
            val base = checkedServer(server.text.toString())
            val result = post(base, "/api/connections/samsung_health/exchange", JSONObject().put("pairing_code", code.text.toString().trim().uppercase()))
            val token = result.getString("device_token")
            prefs.edit().putString("server", base).putString("token", seal(token)).apply()
            code.setText(""); status.text = "Paired. Allow Health Connect access, then tap Sync."
        } }
        button("Allow Health Connect access") {
            if (HealthConnectClient.getSdkStatus(this) == HealthConnectClient.SDK_AVAILABLE) permissionRequest.launch(permissions)
            else status.text = "Health Connect is unavailable. Install or update Health Connect and Samsung Health."
        }
        button("Sync last 7 days") { runTask { sync() } }
        button("Forget pairing on this phone") {
            prefs.edit().clear().apply(); server.setText(""); status.text = "Local pairing removed. Disconnect in Kyrex Connections to revoke the server pairing and erase synced data."
        }
        layout.addView(status)
        setContentView(ScrollView(this).apply { addView(layout) })
    }

    private fun runTask(block: suspend () -> Unit) {
        lifecycleScope.launch {
            status.text = "Working…"
            try { block() } catch (_: Exception) { status.text = "Could not complete this action. Check the server address, pairing, Health Connect permissions and connection, then try again." }
        }
    }

    private fun checkedServer(raw: String): String {
        val u = URI(raw.trim().trimEnd('/'))
        require(u.scheme == "https" && !u.host.isNullOrBlank() && u.userInfo == null && u.query == null && u.fragment == null && (u.path.isNullOrEmpty() || u.path == "/"))
        return u.toString().trimEnd('/')
    }

    private suspend fun post(base: String, path: String, body: JSONObject, token: String? = null): JSONObject = withContext(Dispatchers.IO) {
        val conn = URL(base + path).openConnection() as HttpsURLConnection
        try {
            conn.instanceFollowRedirects = false; conn.requestMethod = "POST"; conn.doOutput = true
            conn.connectTimeout = 20000; conn.readTimeout = 30000
            conn.setRequestProperty("Content-Type", "application/json")
            if (token != null) conn.setRequestProperty("Authorization", "Bearer $token")
            conn.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            check(conn.responseCode in 200..299)
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
    private fun seal(raw: String): String {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding"); cipher.init(Cipher.ENCRYPT_MODE, key())
        return Base64.encodeToString(cipher.iv + cipher.doFinal(raw.toByteArray()), Base64.NO_WRAP)
    }
    private fun token(): String {
        val data = Base64.decode(prefs.getString("token", null) ?: error("Pair first"), Base64.NO_WRAP)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding"); cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, data.copyOfRange(0, 12)))
        return String(cipher.doFinal(data.copyOfRange(12, data.size)), Charsets.UTF_8)
    }

    private suspend fun sync() {
        check(HealthConnectClient.getSdkStatus(this) == HealthConnectClient.SDK_AVAILABLE)
        val client = HealthConnectClient.getOrCreate(this)
        val allowed = client.permissionController.getGrantedPermissions()
        check(allowed.intersect(permissions).isNotEmpty())
        val base = checkedServer(prefs.getString("server", "") ?: "")
        val deviceToken = token()
        val end = Instant.now(); val filter = TimeRangeFilter.between(end.minus(Duration.ofDays(7)), end)
        val batch = mutableListOf<JSONObject>(); var sent = 0
        suspend fun flush() {
            if (batch.isEmpty()) return
            post(base, "/api/connections/samsung_health/sync", JSONObject().put("records", JSONArray(batch)), deviceToken)
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
            } while (page != null)
        }
        if (HealthPermission.getReadPermission(ExerciseSessionRecord::class) in allowed) {
            var page: String? = null
            do {
                val result = client.readRecords(ReadRecordsRequest(ExerciseSessionRecord::class, filter, pageToken = page))
                for (r in result.records) emit(r, "workout", r.startTime, r.endTime, "exercise_type", r.exerciseType)
                page = result.pageToken
            } while (page != null)
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
            } while (page != null)
        }
        if (HealthPermission.getReadPermission(HeartRateRecord::class) in allowed) {
            var page: String? = null
            do {
                val result = client.readRecords(ReadRecordsRequest(HeartRateRecord::class, filter, pageToken = page))
                for (r in result.records) for (sample in r.samples) emit(r, "heart_rate", sample.time, sample.time, "bpm", sample.beatsPerMinute, ":${sample.time}")
                page = result.pageToken
            } while (page != null)
        }
        flush()
        // Empty sync still records the successful contact time.
        post(base, "/api/connections/samsung_health/sync", JSONObject().put("records", JSONArray()).put("complete", true), deviceToken)
        status.text = "Synced $sent Samsung Health readings. ${allowed.intersect(permissions).size} of 4 data permissions granted."
    }
}
