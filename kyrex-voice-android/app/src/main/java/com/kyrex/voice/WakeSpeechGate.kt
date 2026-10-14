package com.kyrex.voice

// Audio-sample clock, rather than wall time, keeps decoder delays consistent.
// All samples still reach keyword spotting; this only accepts/rejects matches.
class WakeSpeechGate(private val enabled: Boolean) {
    private var samplesSeen = 0L
    private var lastSpeech = -16000L
    fun advance(count: Int, speech: Boolean) {
        samplesSeen += count
        if (speech) lastSpeech = samplesSeen
    }
    fun acceptsMatch() = !enabled || samplesSeen - lastSpeech < 8000
}
