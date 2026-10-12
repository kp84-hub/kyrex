package com.kyrex.voice

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.*
import android.text.InputType
import android.view.View
import android.view.inputmethod.EditorInfo
import android.widget.*
import java.util.concurrent.Executors

class MainActivity : Activity() {
    private lateinit var account: VoiceAccount
    private lateinit var server: EditText
    private lateinit var code: EditText
    private lateinit var state: TextView
    private lateinit var feedback: TextView
    private lateinit var diagnostics: TextView
    private lateinit var micMeter: ProgressBar
    private lateinit var voiceLabel: TextView
    private var pendingAction = VoiceService.START
    private val worker = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private val refresh = object : Runnable {
        override fun run() {
            val status = getSharedPreferences("voice-status", MODE_PRIVATE)
            state.text = if (VoiceService.active) status.getString("state", "Listening") else "Stopped"
            micMeter.progress = VoiceService.microphoneLevel
            val natural = NaturalVoice(this@MainActivity)
            voiceLabel.text = if (natural.configured()) "AI reply voice: OpenAI ${NaturalVoice.MODEL} · ${natural.voice()}"
                else "Phone reply voice: ${status.getString("phone_voice", "Android default text-to-speech")}"
            diagnostics.text = buildString {
                append("Wake microphone: ${VoiceService.microphoneLevel}/100\nWake matches: ${VoiceService.wakeMatches}")
                if (VoiceService.lastTranscript.isNotBlank()) append("\nLast heard: ${VoiceService.lastTranscript.take(400)}")
                val issue = status.getString("issue", "").orEmpty()
                if (issue.isNotBlank()) append("\n$issue")
            }
            main.postDelayed(this, 250)
        }
    }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        account = VoiceAccount(this)
        val content = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(32, 64, 32, 32) }
        fun text(value: String) { content.addView(TextView(this).apply { text = value; textSize = 17f; setPadding(0, 8, 0, 12) }) }
        fun button(value: String, action: () -> Unit) { content.addView(Button(this).apply { text = value; setOnClickListener { action() } }) }
        text("Kyrex Voice v${BuildConfig.VERSION_NAME}")
        text("Say Hey Kyrex (hey kai-rex), wait for the tone, then speak your request. You can also tap Talk now to speak without the wake word.")
        state = TextView(this).apply { textSize = 22f }; content.addView(state)
        micMeter = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply { max = 100 }; content.addView(micMeter)
        diagnostics = TextView(this).apply { textSize = 17f }; content.addView(diagnostics)
        voiceLabel = TextView(this).apply { textSize = 16f }; content.addView(voiceLabel)
        feedback = TextView(this).apply { textSize = 16f }; content.addView(feedback)
        button("Reply voice settings") { voiceSettings() }
        button("Microphone and speech privacy") {
            AlertDialog.Builder(this).setTitle("Microphone and speech")
                .setMessage("Wake audio stays on this phone. Android transcribes commands and may use its configured speech service online. Only recognized text goes to Kyrex. If AI reply voice is enabled, reply text goes to OpenAI for AI-generated speech, billed to your API account. The speech key is encrypted on this phone. Listening keeps the microphone and CPU active and uses battery. Stop anytime in the app or notification.")
                .setPositiveButton("OK", null).show()
        }
        server = EditText(this).apply { hint = "Kyrex HTTPS server"; setText(account.origin()) }
        code = EditText(this).apply { hint = "Voice pairing code from Connections" }
        content.addView(server); content.addView(code)
        button("Link voice account") {
            val base = server.text.toString(); val pairingCode = code.text.toString()
            feedback.text = "Pairing…"
            worker.execute {
                val error = try { account.pair(base, pairingCode); null } catch (e: Exception) { e.message ?: "Pairing failed." }
                main.post { if (!isDestroyed) { feedback.text = error ?: "Linked. Tap Start listening."; if (error == null) code.setText("") } }
            }
        }
        button("Start listening") {
            if (!account.linked()) { feedback.text = "Link your voice account first."; return@button }
            AlertDialog.Builder(this).setTitle("Listen for Hey Kyrex?")
                .setMessage("Keep the microphone active for wake detection, including while the screen is off? This uses battery. Android shows a listening notification with Stop. Requests use your existing Kyrex bots and their permissions.")
                .setNegativeButton("Cancel", null).setPositiveButton("Start") { _, _ -> permissions(VoiceService.START) }.show()
        }
        button("Talk now") {
            if (!account.linked()) { feedback.text = "Link your voice account first."; return@button }
            permissions(VoiceService.TALK)
        }
        content.addView(CheckBox(this).apply {
            text = "Sensitive wake detection (restart listening to apply)"
            val options = getSharedPreferences("voice-options", MODE_PRIVATE)
            isChecked = options.getBoolean("sensitive", false)
            setOnCheckedChangeListener { _, checked -> options.edit().putBoolean("sensitive", checked).apply() }
        })
        button("Stop listening") { startService(Intent(this, VoiceService::class.java).setAction(VoiceService.STOP)) }
        button("Open voice chat") {
            if (account.linked()) startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(account.origin() + "/?conversation=" + account.conversation())))
        }
        button("Forget this phone's pairing") {
            stopService(Intent(this, VoiceService::class.java)); account.forget(); NaturalVoice(this).clear(); server.setText(""); feedback.text = "Pairing and AI speech key removed. Disconnect Voice in Chat to revoke the server credential."
        }
        setContentView(ScrollView(this).apply {
            addView(content)
            setOnApplyWindowInsetsListener { view, insets ->
                if (Build.VERSION.SDK_INT >= 30) {
                    val bars = insets.getInsets(android.view.WindowInsets.Type.systemBars())
                    view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
                } else {
                    @Suppress("DEPRECATION")
                    view.setPadding(insets.systemWindowInsetLeft, insets.systemWindowInsetTop, insets.systemWindowInsetRight, insets.systemWindowInsetBottom)
                }
                insets
            }
        })
    }
    private fun voiceSettings() {
        val natural = NaturalVoice(this)
        val fields = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(32, 16, 32, 16) }
        fields.addView(TextView(this).apply {
            text = "AI-generated speech uses OpenAI ${NaturalVoice.MODEL}. Reply text is sent to OpenAI and API usage is billed to your key. The key stays encrypted on this phone. ${if (natural.configured()) "A key is saved; leave the field blank to keep it." else "Enter an OpenAI API key to enable AI speech."}"
        })
        val apiKey = EditText(this).apply {
            hint = "OpenAI API key"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            imeOptions = EditorInfo.IME_FLAG_NO_PERSONALIZED_LEARNING
            importantForAutofill = View.IMPORTANT_FOR_AUTOFILL_NO_EXCLUDE_DESCENDANTS
        }
        fields.addView(apiKey)
        val voices = Spinner(this).apply {
            adapter = ArrayAdapter(this@MainActivity, android.R.layout.simple_spinner_dropdown_item, NaturalVoice.VOICES.map { it.replaceFirstChar(Char::titlecase) })
            setSelection(NaturalVoice.VOICES.indexOf(natural.voice()).coerceAtLeast(0))
        }
        fields.addView(voices)
        val dialog = AlertDialog.Builder(this).setTitle("Reply voice")
            .setView(fields).setNegativeButton("Cancel", null)
            .setNeutralButton("Phone voice") { _, _ -> natural.clear(); feedback.text = "Using your Android text-to-speech voice. AI speech key removed." }
            .setPositiveButton("Save AI voice", null).create()
        dialog.setOnShowListener {
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
                try {
                    natural.configure(apiKey.text.toString(), NaturalVoice.VOICES[voices.selectedItemPosition])
                    apiKey.setText("")
                    feedback.text = "AI voice saved. Tap Talk now to hear the next reply."
                    dialog.dismiss()
                } catch (e: Exception) { apiKey.error = e.message ?: "Speech key could not be saved." }
            }
        }
        dialog.setOnDismissListener { apiKey.setText("") }
        dialog.show()
    }
    private fun permissions(action: String) {
        pendingAction = action
        val missing = mutableListOf<String>()
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) missing.add(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) missing.add(Manifest.permission.POST_NOTIFICATIONS)
        if (missing.isNotEmpty()) requestPermissions(missing.toTypedArray(), 1) else startListening()
    }
    private fun startListening() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) { feedback.text = "Microphone permission is required."; return }
        try { startForegroundService(Intent(this, VoiceService::class.java).setAction(pendingAction)) }
        catch (_: RuntimeException) { feedback.text = "Android could not start listening. Keep this screen open and try again." }
    }
    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == 1) startListening()
    }
    override fun onResume() { super.onResume(); main.post(refresh) }
    override fun onPause() { main.removeCallbacks(refresh); super.onPause() }
    override fun onDestroy() { main.removeCallbacksAndMessages(null); worker.shutdownNow(); super.onDestroy() }
}
