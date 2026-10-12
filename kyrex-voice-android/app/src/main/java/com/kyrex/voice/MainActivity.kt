package com.kyrex.voice

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.*
import android.widget.*
import java.util.concurrent.Executors

class MainActivity : Activity() {
    private lateinit var account: VoiceAccount
    private lateinit var server: EditText
    private lateinit var code: EditText
    private lateinit var state: TextView
    private lateinit var feedback: TextView
    private val worker = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private val refresh = object : Runnable {
        override fun run() { state.text = if (VoiceService.active) getSharedPreferences("voice-status", MODE_PRIVATE).getString("state", "Listening") else "Stopped"; main.postDelayed(this, 1000) }
    }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        account = VoiceAccount(this)
        val content = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(32, 64, 32, 32) }
        fun text(value: String) { content.addView(TextView(this).apply { text = value; textSize = 17f; setPadding(0, 8, 0, 12) }) }
        fun button(value: String, action: () -> Unit) { content.addView(Button(this).apply { text = value; setOnClickListener { action() } }) }
        text("Kyrex Voice v${BuildConfig.VERSION_NAME}")
        text("Say Hey Kyrex, wait for the tone, then speak your request. Kyrex replies aloud and saves the conversation in Chat.")
        text("Wake audio stays on this phone. Android handles request transcription, using its configured speech service when on-device recognition is unavailable. Only recognized text goes to Kyrex. Listening keeps the microphone and CPU active and uses battery. Stop anytime here or in the notification.")
        server = EditText(this).apply { hint = "Kyrex HTTPS server"; setText(account.origin()) }
        code = EditText(this).apply { hint = "Voice pairing code from Connections" }
        content.addView(server); content.addView(code)
        state = TextView(this).apply { textSize = 18f }; content.addView(state)
        feedback = TextView(this).apply { textSize = 16f }; content.addView(feedback)
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
                .setNegativeButton("Cancel", null).setPositiveButton("Start") { _, _ -> permissions() }.show()
        }
        button("Stop listening") { startService(Intent(this, VoiceService::class.java).setAction(VoiceService.STOP)) }
        button("Open voice chat") {
            if (account.linked()) startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(account.origin() + "/?conversation=" + account.conversation())))
        }
        button("Forget this phone's pairing") {
            stopService(Intent(this, VoiceService::class.java)); account.forget(); server.setText(""); feedback.text = "Pairing removed. Disconnect Voice in Chat to revoke the server credential."
        }
        setContentView(ScrollView(this).apply { addView(content) })
    }
    private fun permissions() {
        val missing = mutableListOf<String>()
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) missing.add(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) missing.add(Manifest.permission.POST_NOTIFICATIONS)
        if (missing.isNotEmpty()) requestPermissions(missing.toTypedArray(), 1) else startListening()
    }
    private fun startListening() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) { feedback.text = "Microphone permission is required."; return }
        try { startForegroundService(Intent(this, VoiceService::class.java).setAction(VoiceService.START)) }
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
