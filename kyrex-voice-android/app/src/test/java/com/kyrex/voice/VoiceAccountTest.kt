package com.kyrex.voice

import org.junit.Assert.*
import org.junit.Test

class VoiceAccountTest {
    @Test fun heartbeatDistinguishesTransportFailuresFromLostAuthority() {
        for (status in listOf(408, 429, 500, 502, 503))
            assertTrue(VoiceAccount.responseFailure(status) is java.io.IOException)
        for (status in listOf(401, 403, 404, 409, 426))
            assertTrue(VoiceAccount.responseFailure(status) is VoiceSessionLost)
        assertTrue(VoiceAccount.responseFailure(409, "moved").message!!.contains("took over"))
        assertTrue(VoiceAccount.responseFailure(409, "expired").message!!.contains("expire"))
    }
    @Test fun rejectsCredentialsPathsAndCleartext() {
        for (url in listOf("http://chat.kyrex.dev", "https://user:secret@chat.kyrex.dev", "https://chat.kyrex.dev/api", "https://chat.kyrex.dev?token=x", "https://chat.kyrex.dev#x")) {
            try { VoiceAccount.checkedOrigin(url); fail("Accepted $url") } catch (_: IllegalArgumentException) {}
        }
        assertEquals("https://chat.kyrex.dev", VoiceAccount.checkedOrigin("https://chat.kyrex.dev/"))
    }
    @Test fun spokenRepliesKeepCodeInChatAndBoundLongOutput() {
        val reply = VoiceAccount.spokenText("**Calendar**\n[Monday](https://example.com)\n```text\nPRIVATE CODE\n```")
        assertTrue(reply.contains("Calendar")); assertTrue(reply.contains("Monday"))
        assertFalse(reply.contains("PRIVATE CODE")); assertFalse(reply.contains("https://"))
        assertTrue(VoiceAccount.spokenText("x".repeat(3000)).length < 1700)
    }
}
