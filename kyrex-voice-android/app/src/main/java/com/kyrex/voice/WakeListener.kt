package com.kyrex.voice

import android.annotation.SuppressLint
import android.content.Context
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import com.k2fsa.sherpa.onnx.*
import java.util.concurrent.atomic.AtomicBoolean

open class WakeListener(private val context: Context) {
    private val alive = AtomicBoolean(false)
    @Volatile private var recorder: AudioRecord? = null
    @SuppressLint("MissingPermission") // Service checks the granted microphone permission before starting.
    open fun start(wake: () -> Unit, failure: () -> Unit) {
        if (!alive.compareAndSet(false, true)) return
        Thread({
            var spotter: KeywordSpotter? = null
            var stream: OnlineStream? = null
            var detected = false
            try {
                val model = OnlineModelConfig(transducer = OnlineTransducerModelConfig(
                    encoder = "wake/encoder.onnx", decoder = "wake/decoder.onnx", joiner = "wake/joiner.onnx"),
                    tokens = "wake/tokens.txt", modelType = "zipformer2", numThreads = 1)
                spotter = KeywordSpotter(context.assets, KeywordSpotterConfig(modelConfig = model,
                    keywordsFile = "wake/keywords.txt", keywordsThreshold = 0.35f, keywordsScore = 1.5f))
                stream = spotter.createStream()
                val size = maxOf(4096, AudioRecord.getMinBufferSize(16000, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT))
                val audio = AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, 16000,
                    AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, size)
                recorder = audio
                check(audio.state == AudioRecord.STATE_INITIALIZED)
                if (!alive.get()) return@Thread
                audio.startRecording()
                val samples = ShortArray(1600)
                while (alive.get()) {
                    val count = audio.read(samples, 0, samples.size)
                    check(count > 0)
                    stream.acceptWaveform(FloatArray(count) { samples[it] / 32768.0f }, 16000)
                    while (spotter.isReady(stream)) spotter.decode(stream)
                    if (spotter.getResult(stream).keyword.isNotEmpty()) { detected = true; break }
                }
            } catch (e: Exception) { if (alive.get()) failure() }
            finally {
                try { recorder?.stop() } catch (_: RuntimeException) {}
                recorder?.release(); recorder = null
                stream?.release(); spotter?.release()
                if (alive.getAndSet(false) && detected) wake()
            }
        }, "kyrex-wake").start()
    }
    open fun stop() {
        alive.set(false)
        try { recorder?.stop() } catch (_: RuntimeException) {}
    }
}
