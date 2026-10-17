package com.kyrex.chat

import android.content.Intent
import android.os.Bundle
import com.kyrex.voice.MainActivity
import com.kyrex.voice.VoiceAccount
import com.kyrex.voice.VoiceService

class VoiceActivity : MainActivity() {
    override val integratedChat = true
    override fun connectIntegratedAccount() { NativeSetup.start(this, "voice") }
    override fun forgetIntegratedConnection() { NativeSetup.cancel(this, "voice") }
    override fun openIntegratedChat(voiceSettings: Boolean) {
        startActivity(Intent(this, ChatActivity::class.java).putExtra("voice_settings", voiceSettings))
        finish()
    }
    override fun openIntegratedConversation(conversation: String) {
        startActivity(Intent(this, ChatActivity::class.java).putExtra("voice_conversation", conversation))
        finish()
    }
    override fun onCreate(savedInstanceState: Bundle?) { super.onCreate(savedInstanceState); complete(intent) }
    override fun onNewIntent(intent: Intent) { super.onNewIntent(intent); setIntent(intent); complete(intent) }
    private fun complete(incoming: Intent?) {
        val uri = incoming?.data ?: return
        if (uri.path != "/complete") return
        intent = Intent(this, VoiceActivity::class.java)
        NativeSetup.finish(this, "voice", uri) { result ->
            stopService(Intent(this, VoiceService::class.java))
            VoiceAccount(this).accept(BuildConfig.CHAT_ORIGIN, result)
            android.widget.Toast.makeText(this, "Voice connected. Tap Start listening when ready.", android.widget.Toast.LENGTH_LONG).show()
        }
    }
}
