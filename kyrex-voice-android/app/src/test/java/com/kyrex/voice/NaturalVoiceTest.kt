package com.kyrex.voice

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.net.URL
import java.security.cert.Certificate
import javax.net.ssl.HttpsURLConnection

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class NaturalVoiceTest {
    private class FakeConnection(val status: Int, val bytes: ByteArray) : HttpsURLConnection(URL(NaturalVoice.ENDPOINT)) {
        val body = ByteArrayOutputStream()
        var closed = false
        override fun connect() {}
        override fun disconnect() { closed = true }
        override fun usingProxy() = false
        override fun getCipherSuite() = "test"
        override fun getLocalCertificates(): Array<Certificate>? = null
        override fun getServerCertificates(): Array<Certificate> = emptyArray()
        override fun getResponseCode() = status
        override fun getContentType() = "audio/mpeg"
        override fun getOutputStream() = body
        override fun getInputStream() = ByteArrayInputStream(bytes)
    }
    private fun voice(conn: FakeConnection) = object : NaturalVoice(RuntimeEnvironment.getApplication()) {
        override fun apiKey() = "test-only-speech-credential"
        override fun connection(): HttpsURLConnection = conn
    }
    @Test fun requestUsesOnlyFixedSpeechModelAndAllowedVoices() {
        val request = NaturalVoice.speechRequest("Your calendar is clear.", "cedar")
        assertEquals("gpt-4o-mini-tts", request.getString("model"))
        assertEquals("mp3", request.getString("response_format"))
        assertEquals("Your calendar is clear.", request.getString("input"))
        for ((text, selected) in listOf("" to "cedar", "x".repeat(1701) to "marin", "Hello" to "unlisted")) {
            try { NaturalVoice.speechRequest(text, selected); fail("Invalid request accepted") } catch (_: IllegalArgumentException) {}
        }
    }
    @Test fun responseIsTemporaryAndCredentialNeverEntersRequestBody() {
        val conn = FakeConnection(200, "test-audio".toByteArray())
        val audio = voice(conn).render("Hello Keith.")
        assertEquals("test-audio", audio.readText())
        assertEquals(RuntimeEnvironment.getApplication().cacheDir, audio.parentFile)
        val request = JSONObject(conn.body.toString("UTF-8"))
        assertEquals("Hello Keith.", request.getString("input"))
        assertFalse(request.toString().contains("test-only-speech-credential"))
        assertTrue(conn.closed)
        audio.delete()
    }
    @Test fun oversizedAndRejectedResponsesLeaveNoCachedAudio() {
        val cache = RuntimeEnvironment.getApplication().cacheDir
        val before = cache.list()?.toSet().orEmpty()
        for (conn in listOf(FakeConnection(200, ByteArray(NaturalVoice.MAX_AUDIO_BYTES + 1)), FakeConnection(401, "do not expose provider errors".toByteArray()))) {
            try { voice(conn).render("Hello."); fail("Invalid response accepted") } catch (e: IllegalStateException) {
                assertFalse(e.message.orEmpty().contains("test-only-speech-credential"))
                assertFalse(e.message.orEmpty().contains("do not expose"))
            }
            assertTrue(conn.closed)
            assertEquals(before, cache.list()?.toSet().orEmpty())
        }
    }
    @Test fun providerConnectionCannotRedirectCredentials() {
        val local = object : NaturalVoice(RuntimeEnvironment.getApplication()) { fun inspect() = super.connection() }
        val conn = local.inspect()
        assertEquals("https://api.openai.com/v1/audio/speech", conn.url.toString())
        assertFalse(conn.instanceFollowRedirects)
        conn.disconnect()
    }
}
