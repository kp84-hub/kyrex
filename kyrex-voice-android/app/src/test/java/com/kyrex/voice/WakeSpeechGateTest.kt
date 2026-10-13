package com.kyrex.voice

import org.junit.Assert.*
import org.junit.Test

class WakeSpeechGateTest {
    @Test fun noiseCannotAcceptMatchesWithoutRecentSpeech() {
        val gate = WakeSpeechGate(true)
        assertFalse(gate.acceptsMatch())
        gate.advance(48000, false)
        assertFalse(gate.acceptsMatch())
        gate.advance(1600, true)
        assertTrue(gate.acceptsMatch())
        gate.advance(7500, false)
        assertTrue(gate.acceptsMatch())
        gate.advance(500, false)
        assertFalse(gate.acceptsMatch())
    }
    @Test fun offPreservesKeywordOnlyRecognition() {
        val gate = WakeSpeechGate(false)
        gate.advance(48000, false)
        assertTrue(gate.acceptsMatch())
    }
}
