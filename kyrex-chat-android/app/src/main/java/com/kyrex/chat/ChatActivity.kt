package com.kyrex.chat

import android.net.Uri
import com.google.androidbrowserhelper.trusted.LauncherActivity

class ChatActivity : LauncherActivity() {
    // Incoming intents cannot select a different site or inject native actions.
    override fun getLaunchingUrl(): Uri {
        val base = Uri.parse(BuildConfig.CHAT_ORIGIN + "/").buildUpon()
        if (intent.getBooleanExtra("voice_settings", false)) base.appendQueryParameter("settings", "voice")
        else intent.getStringExtra("voice_conversation")?.takeIf { Regex("[A-Za-z0-9_-]{1,100}").matches(it) }
            ?.let { base.appendQueryParameter("conversation", it) }
        return base.build()
    }
}
