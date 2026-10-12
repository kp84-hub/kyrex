package com.kyrex.voice

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import org.json.JSONObject
import java.net.URI
import java.net.URL
import java.security.KeyStore
import java.util.UUID
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import javax.net.ssl.HttpsURLConnection

class VoiceSessionLost(message: String) : IllegalStateException(message)
class VoiceConnectionUnavailable(message: String) : java.io.IOException(message)

class VoiceAccount(private val context: Context) {
    private val prefs = context.getSharedPreferences("voice-account", Context.MODE_PRIVATE)
    private val alias = "kyrex-voice-account-v1"
    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(alias, null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").apply {
            init(KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build())
        }.generateKey()
    }
    fun linked() = prefs.contains("sealed")
    fun forget() { prefs.edit().clear().commit() }
    fun origin(): String = prefs.getString("origin", "") ?: ""
    fun conversation(): String = prefs.getString("conversation", "") ?: ""
    private fun token(): String {
        val value = JSONObject(prefs.getString("sealed", "") ?: "")
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, Base64.decode(value.getString("iv"), Base64.NO_WRAP)))
        return String(cipher.doFinal(Base64.decode(value.getString("data"), Base64.NO_WRAP)), Charsets.UTF_8)
    }
    private fun connection(base: String, path: String, auth: String? = null): HttpsURLConnection =
        (URL(base + path).openConnection() as HttpsURLConnection).apply {
            instanceFollowRedirects = false
            connectTimeout = 15000; readTimeout = 30000
            setRequestProperty("Accept", "application/json")
            auth?.let { setRequestProperty("Authorization", "Bearer $it") }
        }
    fun pair(server: String, code: String) {
        val base = checkedOrigin(server)
        val conn = connection(base, "/api/voice/pair")
        val value = try {
            conn.requestMethod = "POST"; conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json")
            conn.outputStream.use { it.write(JSONObject().put("pairing_code", code.trim()).toString().toByteArray()) }
            require(conn.responseCode == 200) { "Pairing failed. Generate a new code in Kyrex Connections." }
            JSONObject(conn.inputStream.bufferedReader().use { it.readText() })
        } finally { conn.disconnect() }
        val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.ENCRYPT_MODE, key()) }
        val sealed = JSONObject().put("iv", Base64.encodeToString(cipher.iv, Base64.NO_WRAP))
            .put("data", Base64.encodeToString(cipher.doFinal(value.getString("device_token").toByteArray()), Base64.NO_WRAP))
        check(prefs.edit().putString("origin", base).putString("conversation", value.getString("conversation_id"))
            .putString("sealed", sealed.toString()).commit()) { "Could not save voice pairing." }
    }
    // One submission only. A transport error never replays a request or an action.
    fun ask(text: String, lease: String): String {
        val conn = connection(origin(), "/api/voice/chat", token())
        try {
            conn.requestMethod = "POST"; conn.doOutput = true
            conn.setRequestProperty("X-Voice-Lease", lease)
            conn.setRequestProperty("Content-Type", "application/json")
            conn.setRequestProperty("Accept", "text/event-stream")
            val body = JSONObject().put("message", text).put("request_id", UUID.randomUUID().toString().replace("-", ""))
            conn.outputStream.use { it.write(body.toString().toByteArray()) }
            if (conn.responseCode == 401) throw IllegalStateException("Voice pairing was revoked. Pair again in the app.")
            require(conn.responseCode == 200) { "Kyrex could not accept this request. Check your voice chat." }
            val deadline = System.nanoTime() + 300_000_000_000L
            conn.inputStream.bufferedReader().use { reader ->
                while (System.nanoTime() < deadline) {
                    val line = reader.readLine() ?: break
                    if (!line.startsWith("data: ")) continue
                    val event = JSONObject(line.substring(6))
                    when (event.optString("type")) {
                        "done" -> return spokenText(event.optString("content"))
                        "error" -> return spokenText(event.optString("message", "Kyrex reported an error. Check Chat."))
                        "approval_request" -> return "This needs your approval. Open your voice chat in Kyrex to review it."
                    }
                }
            }
            return "The request is saved in your voice chat. Open Kyrex to check its progress."
        } finally { conn.disconnect() }
    }
    private fun json(path: String, body: JSONObject? = null, lease: String = ""): JSONObject {
        val conn = connection(origin(), path, token())
        try {
            if (path == "/api/voice/heartbeat") { conn.connectTimeout = 5000; conn.readTimeout = 5000 }
            if (lease.isNotEmpty()) conn.setRequestProperty("X-Voice-Lease", lease)
            if (body != null) {
                conn.requestMethod = "POST"; conn.doOutput = true
                conn.setRequestProperty("Content-Type", "application/json")
                conn.outputStream.use { it.write(body.toString().toByteArray()) }
            }
            if (conn.responseCode != 200) throw responseFailure(conn.responseCode, conn.getHeaderField("X-Voice-Reason"))
            return JSONObject(conn.inputStream.bufferedReader().use { it.readText() })
        } finally { conn.disconnect() }
    }
    fun claim() = json("/api/voice/listen", JSONObject().put("name", "Android phone").put("takeover", true)).getString("lease")
    fun renew(lease: String) { json("/api/voice/heartbeat", JSONObject(), lease) }
    fun release(lease: String) { json("/api/voice/stop", JSONObject(), lease) }
    fun settings() = json("/api/voice/settings")
    fun speechConnection(lease: String): HttpsURLConnection = connection(origin(), "/api/voice/speech", token()).apply {
        setRequestProperty("X-Voice-Lease", lease)
    }
    companion object {
        fun responseFailure(status: Int, reason: String? = null): Exception = when (status) {
            401, 403 -> VoiceSessionLost("Voice pairing was revoked or is unavailable. Pair again in Kyrex Chat.")
            409 -> VoiceSessionLost(when (reason) {
                "expired" -> "Voice connection was lost long enough for listening to expire. Start listening again."
                "moved" -> "Another voice session took over listening. Stop voice on that session, then Start listening on this phone."
                "stopped" -> "The voice listening session was stopped. Start listening again."
                else -> "Voice session is no longer available. Check Voice in Kyrex Chat before starting again."
            })
            408, 429 -> VoiceConnectionUnavailable("Voice connection is temporarily unavailable.")
            in 500..599 -> VoiceConnectionUnavailable("Kyrex voice server is temporarily unavailable.")
            else -> VoiceSessionLost("Voice server could not maintain this session (HTTP $status). Check Voice in Kyrex Chat.")
        }
        fun checkedOrigin(raw: String): String {
            val uri = URI(raw.trim())
            require(uri.scheme == "https" && !uri.host.isNullOrEmpty() && uri.rawUserInfo == null
                && uri.rawQuery == null && uri.rawFragment == null && (uri.path.isNullOrEmpty() || uri.path == "/")) {
                "Enter your Kyrex HTTPS server address without a path." }
            return uri.toString().trimEnd('/')
        }
        fun spokenText(raw: String): String = raw.replace(Regex("```[\\s\\S]*?```"), " Details are in your voice chat. ")
            .replace(Regex("\\[([^\\]]+)\\]\\([^)]+\\)"), "$1")
            .replace(Regex("[*#`|]"), "").replace(Regex("\\s+"), " ").trim()
            .let { if (it.length > 1600) it.take(1600) + ". The full response is in your voice chat." else it }
            .ifBlank { "Open your voice chat to review the result." }
    }
}
