package com.kyrex.chat

import android.content.Context
import android.net.Uri
import android.util.Base64
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config
import java.security.MessageDigest

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class HealthHandoffTest {
    private lateinit var context: Context
    private lateinit var handoff: HealthHandoff
    @Before fun prepare() {
        context = RuntimeEnvironment.getApplication()
        context.getSharedPreferences("health_handoff", Context.MODE_PRIVATE).edit().clear().commit()
        // AndroidKeyStore is a platform dependency. Inject only encryption;
        // state, persistence, URL parsing, TTL and proof are production code.
        handoff = HealthHandoff(context, { "sealed:$it" }, { it.removePrefix("sealed:") })
    }
    private fun callback(url: Uri) = Uri.parse("kyrex-chat://health/complete?code=" + "c".repeat(43) + "&state=" + url.getQueryParameter("state"))
    @Test fun proofIsPrivateAndCallbackIsOneUseAcrossActivityRecreation() {
        val url = handoff.begin(1000)
        assertEquals(BuildConfig.CHAT_ORIGIN, "${url.scheme}://${url.authority}")
        assertNull(url.getQueryParameter("verifier"))
        val recreated = HealthHandoff(context, { "sealed:$it" }, { it.removePrefix("sealed:") })
        val (_, verifier) = recreated.consume(callback(url), 2000)
        val challenge = Base64.encodeToString(MessageDigest.getInstance("SHA-256").digest(verifier.toByteArray()), Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING)
        assertEquals(challenge, url.getQueryParameter("challenge"))
        assertThrows(IllegalArgumentException::class.java) { handoff.consume(callback(url), 2000) }
    }
    @Test fun foreignDuplicateAndExpiredCallbacksNeverYieldProof() {
        val url = handoff.begin(1000)
        val valid = callback(url)
        for (raw in listOf(valid.toString().replace("kyrex-chat:", "https:"),
            valid.toString().replace("//health/", "//foreign/"),
            valid.toString().replace("/complete?", "/start?"),
            valid.toString()+"&code="+"d".repeat(43),
            valid.toString()+"&extra=value", valid.toString()+"#fragment",
            valid.toString().replace(url.getQueryParameter("state")!!, "x".repeat(43)))) {
            assertThrows(Exception::class.java) { handoff.consume(Uri.parse(raw), 2000) }
        }
        assertThrows(IllegalArgumentException::class.java) { handoff.consume(valid, 601001) }
        assertEquals("c".repeat(43), handoff.consume(valid, 2000).first)
    }
    @Test fun startingAgainInvalidatesPreviousRequest() {
        val first = handoff.begin(1000)
        val second = handoff.begin(2000)
        assertThrows(IllegalArgumentException::class.java) { handoff.consume(callback(first), 3000) }
        assertEquals("c".repeat(43), handoff.consume(callback(second), 3000).first)
    }
    @Test fun forgettingConnectionInvalidatesPendingReturn() {
        val url = handoff.begin(1000)
        handoff.cancel()
        assertThrows(IllegalArgumentException::class.java) { handoff.consume(callback(url), 2000) }
    }
}
