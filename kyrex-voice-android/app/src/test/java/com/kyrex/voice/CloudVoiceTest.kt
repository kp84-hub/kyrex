package com.kyrex.voice

import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.net.URL
import java.security.cert.Certificate
import javax.net.ssl.HttpsURLConnection
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class CloudVoiceTest {
    private class FakeConnection(val bytes: ByteArray, val status: Int = 200) : HttpsURLConnection(URL("https://chat.kyrex.dev/api/voice/speech")) {
        val body = ByteArrayOutputStream(); var closed = false
        override fun connect() {}
        override fun disconnect() { closed = true }
        override fun usingProxy() = false
        override fun getCipherSuite() = "test"
        override fun getLocalCertificates(): Array<Certificate>? = null
        override fun getServerCertificates(): Array<Certificate> = emptyArray()
        override fun getResponseCode() = status
        override fun getContentType() = "audio/wav"
        override fun getOutputStream() = body
        override fun getInputStream() = ByteArrayInputStream(bytes)
    }
    @Test fun deviceNeverSelectsProviderKeyOrReplyVoice() {
        val conn = FakeConnection("fake speech".toByteArray())
        var seen = ""
        val voice = object : CloudVoice(RuntimeEnvironment.getApplication()) {
            override fun connection(lease: String): HttpsURLConnection { seen = lease; return conn }
        }
        val audio = voice.render("Hello Keith.", "test-only-lease")
        assertEquals("test-only-lease", seen)
        assertEquals("{\"text\":\"Hello Keith.\"}", conn.body.toString("UTF-8"))
        assertEquals("fake speech", audio.readText()); assertTrue(conn.closed); audio.delete()
    }
    @Test fun rejectedAndOversizedResponsesDoNotLeaveAudio() {
        val cache = RuntimeEnvironment.getApplication().cacheDir
        val before = cache.list()?.toSet().orEmpty()
        for (conn in listOf(FakeConnection(ByteArray(CloudVoice.MAX_AUDIO_BYTES + 1)), FakeConnection("private error".toByteArray(), 401))) {
            val voice = object : CloudVoice(RuntimeEnvironment.getApplication()) { override fun connection(lease: String): HttpsURLConnection = conn }
            try { voice.render("Hello.", "test-only-lease"); fail("Invalid reply accepted") } catch (e: IllegalStateException) { assertFalse(e.message.orEmpty().contains("private error")) }
            assertTrue(conn.closed); assertEquals(before, cache.list()?.toSet().orEmpty())
        }
    }
}
