package com.kyrex.chat

import android.app.Activity
import android.net.Uri
import android.widget.Toast
import androidx.browser.customtabs.CustomTabsIntent
import com.kyrex.health.HealthSync
import com.kyrex.health.SyncHttpException
import kotlinx.coroutines.runBlocking
import org.json.JSONObject

object NativeSetup {
    fun start(activity: Activity, capability: String) {
        try { CustomTabsIntent.Builder().build().launchUrl(activity, HealthHandoff(activity, capability = capability).begin()) }
        catch (_: Exception) { Toast.makeText(activity, "Could not open Kyrex setup. Try again.", Toast.LENGTH_LONG).show() }
    }
    fun cancel(activity: Activity, capability: String) { HealthHandoff(activity, capability = capability).cancel() }
    fun finish(activity: Activity, capability: String, uri: Uri, accept: (JSONObject) -> Unit) {
        Thread({
            try {
                val (code, verifier) = HealthHandoff(activity, capability = capability).consume(uri)
                val result = runBlocking { HealthSync(activity).post(BuildConfig.CHAT_ORIGIN, "/api/android/native/exchange",
                    JSONObject().put("capability", capability).put("code", code).put("verifier", verifier)) }
                require(Regex("[A-Za-z0-9-]{1,39}").matches(result.getString("owner")))
                activity.runOnUiThread {
                    if (!activity.isDestroyed) try { accept(result) }
                    catch (_: Exception) { Toast.makeText(activity, "Could not save this connection. Start setup again.", Toast.LENGTH_LONG).show() }
                }
            } catch (error: Exception) { activity.runOnUiThread {
                val message = if (capability == "messages" && error is SyncHttpException && error.statusCode == 409)
                    "Finish the pending confirmed message in Chat before moving Messages, then reconnect."
                    else "Connection expired, changed, or could not finish. Start setup again in Kyrex."
                if (!activity.isDestroyed) Toast.makeText(activity, message, Toast.LENGTH_LONG).show()
            } }
        }, "kyrex-native-connect").start()
    }
}
