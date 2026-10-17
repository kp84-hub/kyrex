// SPDX-License-Identifier: AGPL-3.0-or-later
package com.kyrex.pairing

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.widget.Toast
import com.kyrex.chat.BuildConfig
import com.kyrex.chat.ChatActivity
import com.kyrex.chat.NativeSetup

class IntegratedMessagesActivity : MainActivity() {
    private var pending: Uri? = null
    override fun integratedChat() = true
    override fun connectIntegratedAccount() {
        if (integratedRuntime()?.ready() != true) {
            Toast.makeText(this, "Connect Google Messages first, then link it to Kyrex.", Toast.LENGTH_LONG).show()
            return
        }
        NativeSetup.start(this, "messages")
    }
    override fun openIntegratedChat() { startActivity(Intent(this, ChatActivity::class.java)); finish() }
    override fun forgetIntegratedConnection() { NativeSetup.cancel(this, "messages"); pending = null }
    override fun onCreate(savedInstanceState: Bundle?) {
        pending = intent?.data?.takeIf { it.path == "/complete" }
        super.onCreate(savedInstanceState)
    }
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent); setIntent(intent)
        pending = intent.data?.takeIf { it.path == "/complete" }
        complete()
    }
    override fun onRuntimeReady() { complete() }
    override fun changed() { super.changed(); complete() }
    private fun complete() {
        val uri = pending ?: return
        val runtime = integratedRuntime() ?: return
        if (!runtime.ready() || !runtime.cloudLoaded) return
        pending = null; intent = Intent(this, IntegratedMessagesActivity::class.java)
        NativeSetup.finish(this, "messages", uri) { result ->
            integratedRuntime()?.linkCloudCredential(BuildConfig.CHAT_ORIGIN, result.getString("upload_token"))
        }
    }
}
