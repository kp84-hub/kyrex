package com.kyrex.voice

import android.annotation.SuppressLint
import android.content.Context
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.audiofx.NoiseSuppressor
import android.media.audiofx.AcousticEchoCanceler
import com.k2fsa.sherpa.onnx.*
import java.util.concurrent.atomic.AtomicBoolean

open class WakeListener(private val context: Context) {
    private val alive = AtomicBoolean(false)
    @Volatile private var recorder: AudioRecord? = null
    @Volatile private var captureThread: Thread? = null
    @SuppressLint("MissingPermission") // Service checks the granted microphone permission before starting.
    open fun start(wake: () -> Unit, failure: (String) -> Unit, ready: () -> Unit, level: (Int) -> Unit) {
        if (!alive.compareAndSet(false, true)) return
        captureThread = Thread({
            var spotter: KeywordSpotter? = null
            var stream: OnlineStream? = null
            var vad: Vad? = null
            var suppressor: NoiseSuppressor? = null
            var echoCanceler: AcousticEchoCanceler? = null
            var detected = false
            try {
                android.system.Os.setenv("ORT_DISABLE_TELEMETRY", "1", true)
                val model = OnlineModelConfig(transducer = OnlineTransducerModelConfig(
                    encoder = "wake/encoder.onnx", decoder = "wake/decoder.onnx", joiner = "wake/joiner.onnx"),
                    tokens = "wake/tokens.txt", modelType = "zipformer2", numThreads = 1)
                val sensitive = context.getSharedPreferences("voice-options", Context.MODE_PRIVATE).getBoolean("sensitive", false)
                spotter = KeywordSpotter(context.assets, KeywordSpotterConfig(modelConfig = model,
                    keywordsFile = "wake/keywords.txt", maxActivePaths = 32, numTrailingBlanks = 1, keywordsThreshold = if (sensitive) 0.15f else 0.19f, keywordsScore = 1.0f))
                stream = spotter.createStream()
                val speechFilter = context.getSharedPreferences("voice-options", Context.MODE_PRIVATE)
                    .getString("wake_speech_filter", "off") == "silero"
                val gate = WakeSpeechGate(speechFilter)
                if (speechFilter) vad = Vad(context.assets, VadModelConfig(sileroVadModelConfig = SileroVadModelConfig(
                    model = "wake/silero_vad.onnx", threshold = 0.5f, minSilenceDuration = 0.1f,
                    minSpeechDuration = 0.096f, maxSpeechDuration = 5.0f), numThreads = 1))
                val size = maxOf(4096, AudioRecord.getMinBufferSize(16000, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT))
                val audio = AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, 16000,
                    AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, size)
                recorder = audio
                check(audio.state == AudioRecord.STATE_INITIALIZED)
                // Device effects are optional; unsupported phones still use VAD.
                if (speechFilter) {
                    try { if (NoiseSuppressor.isAvailable()) suppressor = NoiseSuppressor.create(audio.audioSessionId)?.also { it.enabled = true } } catch (_: RuntimeException) {}
                    try { if (AcousticEchoCanceler.isAvailable()) echoCanceler = AcousticEchoCanceler.create(audio.audioSessionId)?.also { it.enabled = true } } catch (_: RuntimeException) {}
                }
                if (!alive.get()) return@Thread
                audio.startRecording()
                val samples = ShortArray(1600)
                var received = 0
                var announced = false
                while (alive.get()) {
                    val count = audio.read(samples, 0, samples.size)
                    check(count > 0) { "Microphone read failed ($count)." }
                    val peak = (0 until count).maxOf { kotlin.math.abs(samples[it].toInt()) }
                    level((peak * 100 / 32768).coerceIn(0, 100))
                    received += count
                    if (!announced && received >= 8000) { announced = true; ready() }
                    val pcm = FloatArray(count) { samples[it] / 32768.0f }
                    vad?.acceptWaveform(pcm)
                    gate.advance(count, vad?.isSpeechDetected() == true)
                    vad?.clear()
                    stream.acceptWaveform(pcm, 16000)
                    while (spotter.isReady(stream)) {
                        spotter.decode(stream)
                        if (announced && spotter.getResult(stream).keyword.isNotEmpty()) {
                            if (gate.acceptsMatch()) { detected = true; break }
                            spotter.reset(stream)
                        }
                    }
                    if (detected) break
                }
            } catch (e: Exception) { if (alive.get()) failure("Wake microphone failed: ${e.javaClass.simpleName}: ${e.message ?: "unknown error"}") }
            finally {
                try { recorder?.stop() } catch (_: RuntimeException) {}
                listOf(suppressor, echoCanceler).forEach { try { it?.release() } catch (_: RuntimeException) {} }
                recorder?.release(); recorder = null
                stream?.release(); spotter?.release(); vad?.release()
                if (alive.getAndSet(false) && detected) wake()
            }
        }, "kyrex-wake").also { it.start() }
    }
    open fun stop() {
        alive.set(false)
        try { recorder?.stop() } catch (_: RuntimeException) {}
    }
    // SpeechRecognizer must not compete with the wake AudioRecord. Wait off
    // the UI thread for the old recorder to release before handing it over.
    open fun stopAndWait(done: (Boolean) -> Unit) {
        stop()
        val previous = captureThread
        Thread({
            previous?.join(5000)
            done(previous?.isAlive != true)
        }, "kyrex-mic-handoff").start()
    }
}
