package com.kyrex.chat

import android.net.Uri
import com.google.androidbrowserhelper.trusted.LauncherActivity

class ChatActivity : LauncherActivity() {
    // Incoming intents cannot select a different site or inject native actions.
    override fun getLaunchingUrl(): Uri = Uri.parse(BuildConfig.CHAT_ORIGIN + "/")
}
