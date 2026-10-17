package com.kyrex.chat

import android.content.Context
import android.net.Uri
import android.util.Base64
import com.kyrex.health.HealthSync
import java.security.MessageDigest
import java.security.SecureRandom

internal class HealthHandoff(context: Context,
    private val seal: (String) -> String = HealthSync(context)::seal,
    private val unseal: (String) -> String = HealthSync(context)::unseal) {
    private val prefs = context.getSharedPreferences("health_handoff", Context.MODE_PRIVATE)
    private val proof = Regex("[A-Za-z0-9_-]{43}")
    private fun random() = Base64.encodeToString(ByteArray(32).also { SecureRandom().nextBytes(it) }, Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING)
    fun cancel() { check(prefs.edit().clear().commit()) }
    fun begin(now: Long = System.currentTimeMillis()): Uri {
        val verifier = random()
        val state = random()
        check(prefs.edit().clear().putString("verifier", seal(verifier))
            .putString("state", state).putLong("expires", now + 600000).commit())
        val challenge = Base64.encodeToString(MessageDigest.getInstance("SHA-256").digest(verifier.toByteArray(Charsets.US_ASCII)), Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING)
        return Uri.parse(BuildConfig.CHAT_ORIGIN + "/").buildUpon()
            .appendQueryParameter("android_health", "1").appendQueryParameter("challenge", challenge)
            .appendQueryParameter("state", state).build()
    }
    fun consume(uri: Uri, now: Long = System.currentTimeMillis()): Pair<String, String> {
        require(uri.scheme == "kyrex-chat" && uri.host == "health" && uri.path == "/complete" &&
            uri.port == -1 && uri.userInfo == null && uri.fragment == null &&
            uri.queryParameterNames == setOf("code", "state"))
        val code = uri.getQueryParameters("code").single()
        val state = uri.getQueryParameters("state").single()
        require(proof.matches(code) && proof.matches(state))
        require(now < prefs.getLong("expires", 0) && MessageDigest.isEqual(
            state.toByteArray(), (prefs.getString("state", "") ?: "").toByteArray()))
        val verifier = unseal(prefs.getString("verifier", null) ?: error("Start setup again"))
        check(prefs.edit().clear().commit())
        return code to verifier
    }
}
