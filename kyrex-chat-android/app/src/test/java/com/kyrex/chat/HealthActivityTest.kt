package com.kyrex.chat

import android.widget.EditText
import android.widget.TextView
import android.widget.Button
import android.view.View
import android.view.ViewGroup
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class HealthActivityTest {
    @Test fun integratedSettingsHaveNoServerOrPairingCodeInputs() {
        val controller = Robolectric.buildActivity(HealthActivity::class.java).setup()
        val activity = controller.get()
        fun walk(view: View): List<View> = listOf(view) + if (view is ViewGroup) (0 until view.childCount).flatMap { walk(view.getChildAt(it)) } else emptyList()
        val views = walk(activity.window.decorView)
        assertTrue(views.none { it is EditText })
        assertTrue(views.any { it is TextView && it.text.toString() == "Back to Kyrex Chat" })
        assertTrue(views.any { it is TextView && it.text.toString() == "Connect health to Kyrex" })
        assertFalse(activity.getSharedPreferences("pairing", 0).getBoolean("auto_sync", false))
        (views.first { it is Button && it.text.toString() == "Sync last 7 days" } as Button).performClick()
        org.robolectric.Shadows.shadowOf(android.os.Looper.getMainLooper()).idle()
        assertTrue(views.any { it is TextView && it.text.toString().startsWith("Connect health to Kyrex first") })
        controller.pause().stop().destroy()
    }
    @Test fun assetStatementMatchesConfiguredTrustedOrigin() {
        val context = org.robolectric.RuntimeEnvironment.getApplication()
        val entries = org.json.JSONArray(context.getString(com.kyrex.chat.R.string.asset_statements))
        assertEquals(BuildConfig.CHAT_ORIGIN, entries.getJSONObject(0).getJSONObject("target").getString("site"))
    }
}
