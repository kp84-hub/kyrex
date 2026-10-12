package com.kyrex.voice

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import org.json.JSONObject
import java.io.File
import java.net.URL
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import javax.net.ssl.HttpsURLConnection

/** Reply speech only. Wake audio and command audio never enter this API. */
open class NaturalVoice(private val context: Context) {
    private val prefs = context.getSharedPreferences("natural-voice", Context.MODE_PRIVATE)
    private val alias = "kyrex-natural-voice-v1"
    open fun configured() = prefs.contains("sealed")
    fun voice() = prefs.getString("voice", "cedar") ?: "cedar"
    fun clear() { prefs.edit().clear().commit() }
    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(alias, null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").apply {
            init(KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build())
        }.generateKey()
    }
    fun configure(rawKey: String, selectedVoice: String) {
        require(selectedVoice in VOICES) { "Choose Cedar or Marin." }
        val apiKey = rawKey.trim()
        if (apiKey.isEmpty()) {
            check(configured()) { "Enter your OpenAI API key." }
            check(prefs.edit().putString("voice", selectedVoice).commit())
            return
        }
        require(apiKey.length in 20..512 && apiKey.none { it.isWhitespace() }) { "Enter a valid OpenAI API key." }
        val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.ENCRYPT_MODE, key()) }
        val sealed = JSONObject().put("iv", Base64.encodeToString(cipher.iv, Base64.NO_WRAP))
            .put("data", Base64.encodeToString(cipher.doFinal(apiKey.toByteArray()), Base64.NO_WRAP))
        check(prefs.edit().putString("sealed", sealed.toString()).putString("voice", selectedVoice).commit())
    }
    protected open fun apiKey(): String = try {
        val value = JSONObject(prefs.getString("sealed", "") ?: "")
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, Base64.decode(value.getString("iv"), Base64.NO_WRAP)))
        String(cipher.doFinal(Base64.decode(value.getString("data"), Base64.NO_WRAP)), Charsets.UTF_8)
    } catch (_: Exception) { throw IllegalStateException("AI speech key could not be read. Enter it again in Reply voice settings.") }
    protected open fun connection(): HttpsURLConnection =
        (URL(ENDPOINT).openConnection() as HttpsURLConnection).apply {
            instanceFollowRedirects = false
            connectTimeout = 15000; readTimeout = 45000
        }
    // One synthesis request, with a fixed endpoint and no automatic retries.
    open fun render(text: String): File {
        val request = speechRequest(text, voice())
        val secret = apiKey()
        val conn = connection()
        val deadline = System.nanoTime() + 90_000_000_000L
        var output: File? = null
        var complete = false
        try {
            conn.requestMethod = "POST"; conn.doOutput = true
            conn.setRequestProperty("Authorization", "Bearer $secret")
            conn.setRequestProperty("Content-Type", "application/json")
            conn.setRequestProperty("Accept", "audio/mpeg")
            conn.outputStream.use { it.write(request.toString().toByteArray()) }
            if (conn.responseCode != 200) throw IllegalStateException(statusError(conn.responseCode))
            check(conn.contentType?.startsWith("audio/") == true) { "AI speech returned an unexpected format. Check your voice chat." }
            val audio = File.createTempFile("kyrex-ai-", ".mp3", context.cacheDir)
            output = audio
            conn.inputStream.use { source -> audio.outputStream().use { target ->
                val buffer = ByteArray(8192)
                var total = 0
                while (true) {
                    check(System.nanoTime() < deadline) { "AI speech took too long. Your reply is saved in voice chat." }
                    val count = source.read(buffer)
                    if (count < 0) break
                    total += count
                    check(total <= MAX_AUDIO_BYTES) { "AI speech response was too large. Check your voice chat." }
                    target.write(buffer, 0, count)
                }
                check(total > 0) { "AI speech returned no audio." }
            } }
            complete = true
            return audio
        } finally {
            conn.disconnect()
            if (!complete) output?.delete()
        }
    }
    companion object {
        const val MODEL = "gpt-4o-mini-tts"
        const val ENDPOINT = "https://api.openai.com/v1/audio/speech"
        const val MAX_AUDIO_BYTES = 4 * 1024 * 1024
        val VOICES = listOf("cedar", "marin")
        fun speechRequest(text: String, voice: String): JSONObject {
            require(voice in VOICES) { "Choose Cedar or Marin." }
            require(text.isNotBlank() && text.length <= 1700) { "AI speech needs a short reply." }
            return JSONObject().put("model", MODEL).put("voice", voice).put("input", text)
                .put("response_format", "mp3")
                .put("instructions", "Speak naturally, warmly and clearly, as in a relaxed conversation. Use natural pauses and a normal pace. Read the supplied text; do not add or change information.")
        }
        fun statusError(status: Int): String = when (status) {
            401, 403 -> "OpenAI rejected the speech API key. Check Reply voice settings."
            402, 429 -> "OpenAI speech quota or rate limit reached. Check your API account."
            else -> "AI speech is unavailable. Your reply is saved in voice chat."
        }
    }
}
