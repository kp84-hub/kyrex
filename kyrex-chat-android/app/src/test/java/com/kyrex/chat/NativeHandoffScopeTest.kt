package com.kyrex.chat

import android.content.Context
import android.net.Uri
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class NativeHandoffScopeTest {
    @Test fun eachCapabilityHasItsOwnProofAndCancellation() {
        val context = RuntimeEnvironment.getApplication() as Context
        val handoffs = listOf("health", "voice", "messages").associateWith {
            HealthHandoff(context, { "sealed:$it" }, { it.removePrefix("sealed:") }, it)
        }
        val urls = handoffs.mapValues { it.value.begin(1000) }
        val voice = Uri.parse("kyrex-chat://voice/complete?code="+"c".repeat(43)+"&state="+urls.getValue("voice").getQueryParameter("state"))
        assertThrows(IllegalArgumentException::class.java) { handoffs.getValue("messages").consume(voice,2000) }
        handoffs.getValue("health").cancel()
        assertEquals("c".repeat(43), handoffs.getValue("voice").consume(voice,2000).first)
        val messages = Uri.parse("kyrex-chat://messages/complete?code="+"c".repeat(43)+"&state="+urls.getValue("messages").getQueryParameter("state"))
        assertEquals("c".repeat(43), handoffs.getValue("messages").consume(messages,2000).first)
    }
}
