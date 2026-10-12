package com.kyrex.voice

import android.content.Context
import org.json.JSONObject
import java.io.File
import javax.net.ssl.HttpsURLConnection

/** Shared reply voice. A provider API key is never downloaded to this phone. */
open class CloudVoice(private val context: Context) {
    protected open fun connection(lease: String): HttpsURLConnection = VoiceAccount(context).speechConnection(lease)
    open fun render(text: String, lease: String): File {
        val request = speechRequest(text)
        val conn = connection(lease)
        var output: File? = null
        var complete = false
        val deadline = System.nanoTime() + 90_000_000_000L
        try {
            conn.requestMethod = "POST"; conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json")
            conn.outputStream.use { it.write(request.toString().toByteArray()) }
            if (conn.responseCode != 200) throw IllegalStateException("Shared AI speech failed. Check Voice settings and your voice chat.")
            check(conn.contentType?.startsWith("audio/") == true) { "AI speech returned an unexpected format. Check your voice chat." }
            val file = File.createTempFile("kyrex-ai-", ".wav", context.cacheDir); output = file
            conn.inputStream.use { source -> file.outputStream().use { target ->
                val buffer = ByteArray(8192); var total = 0
                while (true) {
                    check(System.nanoTime() < deadline) { "AI speech took too long. Check your voice chat." }
                    val count = source.read(buffer); if (count < 0) break
                    total += count; check(total <= MAX_AUDIO_BYTES) { "AI speech response was too large. Check your voice chat." }
                    target.write(buffer, 0, count)
                }
                check(total > 0) { "AI speech returned no audio." }
            } }
            complete = true; return file
        } finally { conn.disconnect(); if (!complete) output?.delete() }
    }
    companion object {
        const val MAX_AUDIO_BYTES = 4 * 1024 * 1024
        fun speechRequest(text: String): JSONObject {
            require(text.isNotBlank() && text.length <= 1700) { "AI speech needs a short reply." }
            return JSONObject().put("text", text)
        }
    }
}
