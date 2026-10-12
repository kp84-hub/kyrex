package com.kyrex.voice

import android.Manifest
import android.annotation.SuppressLint
import android.app.*
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.media.*
import android.os.*
import android.speech.*
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import java.io.File
import java.util.Locale
import java.util.concurrent.Executors

open class VoiceService : Service(), RecognitionListener {
    private val main = Handler(Looper.getMainLooper())
    private val worker = Executors.newSingleThreadExecutor()
    private var listener: WakeListener? = null
    private var recognizer: SpeechRecognizer? = null
    private var speaker: TextToSpeech? = null
    private var speakerReady = false
    @Volatile private var running = false
    @Volatile private var generation = 0
    private var turn = 0
    private var speakingId: String? = null
    private var singleTurn = false
    private var audioPlayer: MediaPlayer? = null
    private var replyAudio: File? = null
    private var power: PowerManager.WakeLock? = null
    private var mode = "Stopped"
    private val timeout = Runnable { recognizer?.cancel(); issue("No speech result arrived. Try Talk now again."); resumeWake() }
    override fun onBind(intent: Intent?) = null

    override fun onCreate() {
        super.onCreate()
        active = false
        cacheDir.listFiles()?.filter { it.name.startsWith("kyrex-ai-") && it.extension == "mp3" }?.forEach { it.delete() }
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel("kyrex_voice", "Kyrex voice listening", NotificationManager.IMPORTANCE_LOW))
        speaker = TextToSpeech(this) { status ->
            speakerReady = status == TextToSpeech.SUCCESS
            if (speakerReady) {
                speaker?.language = Locale.US
                getSharedPreferences("voice-status", MODE_PRIVATE).edit()
                    .putString("phone_voice", "${speaker?.defaultEngine ?: "Android"} · ${speaker?.voice?.name ?: "default voice"}").apply()
            }
        }
        speaker?.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(id: String?) {}
            override fun onDone(id: String?) { main.post { if (running && id != null && id == speakingId) resumeWake() } }
            @Deprecated("Legacy callback") override fun onError(id: String?) { main.post { if (running && id != null && id == speakingId) resumeWake() } }
        })
    }

    @SuppressLint("WakelockTimeout") // Explicit user-started listening; Stop/onDestroy releases it.
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val action = intent?.action
        if (action == STOP) { stopListening(); return START_NOT_STICKY }
        if (action !in listOf(START, TALK) || !linked()
            || checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            stopSelf(); return START_NOT_STICKY
        }
        if (running) {
            if (action == TALK && mode in listOf("Starting wake listening…", "Listening for Hey Kyrex")) capture()
            return START_NOT_STICKY
        }
        try {
            issue(""); lastTranscript = ""; wakeMatches = 0; microphoneLevel = 0
            singleTurn = action == TALK
            if (Build.VERSION.SDK_INT >= 29) startForeground(1, notification("Starting voice…"), ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
            else startForeground(1, notification("Starting voice…"))
            running = true; active = true; generation++
            power = getSystemService(PowerManager::class.java).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "kyrex:voice").also { it.acquire() }
            if (singleTurn) capture() else resumeWake()
        } catch (e: RuntimeException) { fail("Could not start listening: ${e.javaClass.simpleName}. Open Kyrex Voice and try again.") }
        // Android may terminate this session. Never silently restart microphone capture.
        return START_NOT_STICKY
    }

    private fun notification(text: String): Notification {
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val stop = PendingIntent.getService(this, 1, Intent(this, VoiceService::class.java).setAction(STOP), PendingIntent.FLAG_IMMUTABLE)
        return Notification.Builder(this, "kyrex_voice").setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentTitle("Kyrex Voice").setContentText(text).setContentIntent(open).setOngoing(true)
            .setOnlyAlertOnce(true).setVisibility(Notification.VISIBILITY_PRIVATE)
            .addAction(Notification.Action.Builder(null, "Stop listening", stop).build()).build()
    }
    private fun update(value: String) {
        mode = value
        getSharedPreferences("voice-status", MODE_PRIVATE).edit().putString("state", value).apply()
        if (running && (Build.VERSION.SDK_INT < 33 || checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED))
            getSystemService(NotificationManager::class.java).notify(1, notification(value))
    }
    private fun issue(value: String) { getSharedPreferences("voice-status", MODE_PRIVATE).edit().putString("issue", value).apply() }
    private fun fail(value: String) { issue(value); stopListening() }
    private fun resumeWake() {
        if (!running) return
        main.removeCallbacks(timeout)
        recognizer?.destroy(); recognizer = null
        if (singleTurn) { stopListening(); return }
        listener?.stop()
        speakingId = null
        update("Starting wake listening…")
        val token = ++generation
        listener = createWakeListener().also { it.start(
            { main.post { if (running && generation == token) { wakeMatches++; capture() } } },
            { error -> main.post { if (running && generation == token) fail(error) } },
            { main.post { if (running && generation == token && mode == "Starting wake listening…") update("Listening for Hey Kyrex") } },
            { value -> main.post { if (running && generation == token) microphoneLevel = value } }) }
    }
    private fun capture() {
        if (!running) return
        val previous = listener; listener = null
        val token = ++generation
        update("Preparing speech microphone…")
        val ready: (Boolean) -> Unit = { released -> main.post {
            if (running && generation == token) {
                microphoneLevel = 0
                if (!released) fail("The wake microphone did not release. Stop listening and try again.")
                else { update("Tell me your request"); beginSpeechCapture() }
            }
        } }
        if (previous != null) previous.stopAndWait(ready) else ready(true)
    }
    protected open fun beginSpeechCapture() {
        if (!SpeechRecognizer.isRecognitionAvailable(this)) {
            fail("Android speech recognition is unavailable. Enable your phone's speech recognition service."); return
        }
        val token = generation
        try {
            recognizer = if (Build.VERSION.SDK_INT >= 31 && SpeechRecognizer.isOnDeviceRecognitionAvailable(this))
                SpeechRecognizer.createOnDeviceSpeechRecognizer(this) else SpeechRecognizer.createSpeechRecognizer(this)
            recognizer!!.setRecognitionListener(object : RecognitionListener by this@VoiceService {
                override fun onResults(results: Bundle?) { if (running && generation == token) this@VoiceService.onResults(results) }
                override fun onError(error: Int) { if (running && generation == token) this@VoiceService.onError(error) }
                override fun onReadyForSpeech(params: Bundle?) { if (running && generation == token) this@VoiceService.onReadyForSpeech(params) }
            })
            val speech = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
                .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
                .putExtra(RecognizerIntent.EXTRA_LANGUAGE, "en-US")
                .putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
            recognizer!!.startListening(speech); main.postDelayed(timeout, 30000)
        } catch (e: RuntimeException) { issue("Speech recognition could not start: ${e.javaClass.simpleName}."); resumeWake() }
    }
    override fun onResults(results: Bundle?) {
        main.removeCallbacks(timeout)
        val text = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()?.trim().orEmpty()
        if (!running || mode != "Tell me your request") return
        if (text.isBlank()) { resumeWake(); return }
        lastTranscript = text
        issue("")
        update("Kyrex is working…")
        val token = generation
        val requestTurn = ++turn
        recognizer?.destroy(); recognizer = null
        worker.execute {
            val result = try { ask(text) }
                catch (e: Exception) {
                    val message = e.message?.takeIf { e is IllegalStateException } ?: "The connection failed. Check your voice chat before repeating the request."
                    main.post { if (running && token == generation) issue(message) }
                    message
                }
            main.post {
                if (!running || token != generation) return@post
                if (createNaturalVoice().configured()) speakNatural(result, token)
                else {
                    update("Speaking the reply")
                    speakingId = "$token:$requestTurn"
                    if (speakerReady && speaker?.speak(result, TextToSpeech.QUEUE_FLUSH, null, speakingId) == TextToSpeech.SUCCESS) return@post
                    issue("Spoken output is unavailable. Check your voice chat.")
                    main.postDelayed({ if (running && token == generation) resumeWake() }, 2500)
                }
            }
        }
    }
    private fun releaseReplyAudio() {
        audioPlayer?.release(); audioPlayer = null
        replyAudio?.delete(); replyAudio = null
    }
    private fun speakNatural(text: String, token: Int) {
        update("Generating AI reply voice…")
        worker.execute {
            if (!running || token != generation) return@execute
            var audio: File? = null
            val error = try { audio = createNaturalVoice().render(text); null }
                catch (e: Exception) { if (e is IllegalStateException) e.message else "AI speech connection failed. Your reply is saved in voice chat." }
            val file = audio
            main.post {
                if (!running || token != generation) { file?.delete(); return@post }
                if (file == null) { issue(error ?: "AI speech failed. Check your voice chat."); resumeWake(); return@post }
                releaseReplyAudio()
                val player = MediaPlayer()
                audioPlayer = player; replyAudio = file
                try {
                    player.setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ASSISTANT)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                    player.setOnPreparedListener {
                        if (running && generation == token && audioPlayer === player) {
                            update("Speaking with AI voice"); player.start()
                        }
                    }
                    player.setOnCompletionListener {
                        if (running && generation == token && audioPlayer === player) {
                            releaseReplyAudio(); resumeWake()
                        }
                    }
                    player.setOnErrorListener { _, _, _ ->
                        if (running && generation == token && audioPlayer === player) {
                            issue("AI reply audio could not play. Check your voice chat."); releaseReplyAudio(); resumeWake()
                        }
                        true
                    }
                    player.setDataSource(file.absolutePath)
                    player.prepareAsync()
                } catch (_: RuntimeException) {
                    issue("AI reply audio could not play. Check your voice chat."); releaseReplyAudio(); resumeWake()
                } catch (_: java.io.IOException) {
                    issue("AI reply audio could not play. Check your voice chat."); releaseReplyAudio(); resumeWake()
                }
            }
        }
    }
    private fun stopListening() {
        running = false; active = false; generation++; speakingId = null; microphoneLevel = 0
        main.removeCallbacks(timeout)
        listener?.stop(); listener = null
        recognizer?.cancel(); recognizer?.destroy(); recognizer = null
        speaker?.stop()
        releaseReplyAudio()
        power?.let { if (it.isHeld) it.release() }; power = null
        getSharedPreferences("voice-status", MODE_PRIVATE).edit().putString("state", "Stopped").apply()
        stopForeground(STOP_FOREGROUND_REMOVE); stopSelf()
    }
    override fun onDestroy() {
        stopListening(); main.removeCallbacksAndMessages(null)
        speaker?.shutdown(); speaker = null
        worker.shutdownNow(); super.onDestroy()
    }
    override fun onError(error: Int) {
        if (running && mode == "Tell me your request") {
            issue(speechError(error)); resumeWake()
        }
    }
    override fun onReadyForSpeech(params: Bundle?) {
        if (!running || mode != "Tell me your request") return
        ToneGenerator(AudioManager.STREAM_MUSIC, 60).let { tone ->
            tone.startTone(ToneGenerator.TONE_PROP_BEEP, 120); main.postDelayed({ tone.release() }, 200)
        }
    }
    override fun onBeginningOfSpeech() {}
    override fun onRmsChanged(rmsdB: Float) {}
    override fun onBufferReceived(buffer: ByteArray?) {}
    override fun onEndOfSpeech() {}
    override fun onPartialResults(results: Bundle?) {}
    override fun onEvent(eventType: Int, params: Bundle?) {}
    protected open fun ask(text: String) = VoiceAccount(this).ask(text)
    protected open fun createNaturalVoice() = NaturalVoice(this)
    protected open fun linked() = VoiceAccount(this).linked()
    protected open fun createWakeListener() = WakeListener(this)
    companion object {
        const val START = "com.kyrex.voice.START"
        const val STOP = "com.kyrex.voice.STOP"
        const val TALK = "com.kyrex.voice.TALK"
        @Volatile var active = false; private set
        @Volatile var microphoneLevel = 0; private set
        @Volatile var wakeMatches = 0; private set
        @Volatile var lastTranscript = ""; private set
        fun speechError(error: Int): String = when (error) {
            SpeechRecognizer.ERROR_NO_MATCH -> "No speech was recognized. Wait for the tone, then speak."
            SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> "No speech was heard. Check the microphone and try Talk now."
            SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> "Microphone permission is missing. Enable it in Android app settings."
            SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> "Android speech recognition is busy. Close other voice apps and try again."
            SpeechRecognizer.ERROR_NETWORK, SpeechRecognizer.ERROR_NETWORK_TIMEOUT -> "Android speech recognition could not reach its service. Check the phone connection."
            SpeechRecognizer.ERROR_LANGUAGE_NOT_SUPPORTED, SpeechRecognizer.ERROR_LANGUAGE_UNAVAILABLE -> "English speech recognition is unavailable. Download English in your phone's speech service."
            else -> "Android speech recognition failed (code $error). Try Talk now again."
        }
    }
}
