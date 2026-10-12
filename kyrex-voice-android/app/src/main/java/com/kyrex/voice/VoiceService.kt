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
import java.util.Locale
import java.util.concurrent.Executors

open class VoiceService : Service(), RecognitionListener {
    private val main = Handler(Looper.getMainLooper())
    private val worker = Executors.newSingleThreadExecutor()
    private var listener: WakeListener? = null
    private var recognizer: SpeechRecognizer? = null
    private var speaker: TextToSpeech? = null
    private var speakerReady = false
    private var running = false
    private var generation = 0
    private var turn = 0
    private var speakingId: String? = null
    private var power: PowerManager.WakeLock? = null
    private var mode = "Stopped"
    private val timeout = Runnable { recognizer?.cancel(); resumeWake() }
    override fun onBind(intent: Intent?) = null

    override fun onCreate() {
        super.onCreate()
        active = false
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel("kyrex_voice", "Kyrex voice listening", NotificationManager.IMPORTANCE_LOW))
        speaker = TextToSpeech(this) { status ->
            speakerReady = status == TextToSpeech.SUCCESS
            if (speakerReady) speaker?.language = Locale.US
        }
        speaker?.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(id: String?) {}
            override fun onDone(id: String?) { main.post { if (running && id != null && id == speakingId) resumeWake() } }
            @Deprecated("Legacy callback") override fun onError(id: String?) { main.post { if (running && id != null && id == speakingId) resumeWake() } }
        })
    }

    @SuppressLint("WakelockTimeout") // Explicit user-started listening; Stop/onDestroy releases it.
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == STOP) { stopListening(); return START_NOT_STICKY }
        if (intent?.action != START || !linked()
            || checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            stopSelf(); return START_NOT_STICKY
        }
        if (running) return START_NOT_STICKY
        try {
            if (Build.VERSION.SDK_INT >= 29) startForeground(1, notification("Starting voice…"), ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
            else startForeground(1, notification("Starting voice…"))
            running = true; active = true; generation++
            power = getSystemService(PowerManager::class.java).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "kyrex:voice").also { it.acquire() }
            resumeWake()
        } catch (_: RuntimeException) { update("Could not start listening. Open Kyrex Voice."); stopListening() }
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
    private fun resumeWake() {
        if (!running) return
        main.removeCallbacks(timeout)
        recognizer?.destroy(); recognizer = null
        listener?.stop()
        speakingId = null
        update("Starting wake listening…")
        val token = generation
        listener = createWakeListener().also { it.start(
            { main.post { if (running && generation == token) capture() } },
            { main.post { if (running && generation == token) { update("Microphone or wake model unavailable. Restart listening."); stopListening() } } },
            { main.post { if (running && generation == token && mode == "Starting wake listening…") update("Listening for Hey Kyrex") } }) }
    }
    private fun capture() {
        if (!running) return
        listener?.stop(); listener = null
        if (!SpeechRecognizer.isRecognitionAvailable(this)) {
            update("Android speech recognition is unavailable."); stopListening(); return
        }
        recognizer = if (Build.VERSION.SDK_INT >= 31 && SpeechRecognizer.isOnDeviceRecognitionAvailable(this))
            SpeechRecognizer.createOnDeviceSpeechRecognizer(this) else SpeechRecognizer.createSpeechRecognizer(this)
        recognizer!!.setRecognitionListener(this)
        update("Tell me your request")
        val speech = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
            .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            .putExtra(RecognizerIntent.EXTRA_LANGUAGE, "en-US")
            .putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
        try { recognizer!!.startListening(speech); main.postDelayed(timeout, 30000) }
        catch (_: RuntimeException) { resumeWake() }
    }
    override fun onResults(results: Bundle?) {
        main.removeCallbacks(timeout)
        val text = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()?.trim().orEmpty()
        if (!running || mode != "Tell me your request") return
        if (text.isBlank()) { resumeWake(); return }
        update("Kyrex is working…")
        val token = generation
        val requestTurn = ++turn
        recognizer?.destroy(); recognizer = null
        worker.execute {
            val result = try { VoiceAccount(this).ask(text) }
                catch (_: Exception) { "The connection failed. Check your voice chat before repeating the request." }
            main.post {
                if (!running || token != generation) return@post
                update("Speaking the reply")
                speakingId = "$token:$requestTurn"
                if (speakerReady && speaker?.speak(result, TextToSpeech.QUEUE_FLUSH, null, speakingId) == TextToSpeech.SUCCESS) return@post
                update("Spoken output is unavailable. Check your voice chat.")
                main.postDelayed({ if (running && token == generation) resumeWake() }, 2500)
            }
        }
    }
    private fun stopListening() {
        running = false; active = false; generation++; speakingId = null
        main.removeCallbacks(timeout)
        listener?.stop(); listener = null
        recognizer?.cancel(); recognizer?.destroy(); recognizer = null
        speaker?.stop()
        power?.let { if (it.isHeld) it.release() }; power = null
        getSharedPreferences("voice-status", MODE_PRIVATE).edit().putString("state", "Stopped").apply()
        stopForeground(STOP_FOREGROUND_REMOVE); stopSelf()
    }
    override fun onDestroy() {
        stopListening(); main.removeCallbacksAndMessages(null)
        speaker?.shutdown(); speaker = null
        worker.shutdownNow(); super.onDestroy()
    }
    override fun onError(error: Int) { if (running && mode == "Tell me your request") resumeWake() }
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
    protected open fun linked() = VoiceAccount(this).linked()
    protected open fun createWakeListener() = WakeListener(this)
    companion object {
        const val START = "com.kyrex.voice.START"
        const val STOP = "com.kyrex.voice.STOP"
        @Volatile var active = false; private set
    }
}
