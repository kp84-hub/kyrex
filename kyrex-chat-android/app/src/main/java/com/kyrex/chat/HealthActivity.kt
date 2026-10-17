package com.kyrex.chat

import android.content.Intent
import android.os.Bundle
import androidx.browser.customtabs.CustomTabsIntent
import com.kyrex.health.HealthSetupException
import com.kyrex.health.HealthSync
import com.kyrex.health.MainActivity
import org.json.JSONObject

class HealthActivity : MainActivity() {
    override val integratedChat = true
    override fun startIntegratedConnection() {
        runTask("Open health setup") {
            val url = HealthHandoff(this@HealthActivity).begin()
            CustomTabsIntent.Builder().build().launchUrl(this@HealthActivity, url)
        }
    }
    override fun returnToChat() {
        startActivity(Intent(this, ChatActivity::class.java))
        finish()
    }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        complete(intent)
    }
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        complete(intent)
    }
    private fun complete(incoming: Intent?) {
        val uri = incoming?.data ?: return
        if (uri.path != "/complete") return
        // Do not retain a callback credential in the activity's saved intent.
        intent = Intent(this, HealthActivity::class.java)
        runTask("Connect health") {
            val (code, verifier) = try { HealthHandoff(this@HealthActivity).consume(uri) }
                catch (_: Exception) { throw HealthSetupException("Connection expired or changed. Open health setup again.") }
            val result = HealthSync(this@HealthActivity).post(BuildConfig.CHAT_ORIGIN,
                "/api/android/health/exchange", JSONObject().put("code", code).put("verifier", verifier))
            val token = result.getString("device_token")
            val owner = result.getString("owner")
            require(Regex("[A-Za-z0-9_-]{40,100}").matches(token) && Regex("[A-Za-z0-9-]{1,39}").matches(owner))
            completeIntegratedPairing(BuildConfig.CHAT_ORIGIN, token, owner)
        }
    }
}
