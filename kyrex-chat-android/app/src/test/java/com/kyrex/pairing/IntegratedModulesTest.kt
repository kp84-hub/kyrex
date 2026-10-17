// SPDX-License-Identifier: AGPL-3.0-or-later
package com.kyrex.pairing

import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.view.View
import android.view.ViewGroup
import android.widget.EditText
import android.widget.TextView
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config
import org.robolectric.util.ReflectionHelpers
import com.kyrex.chat.VoiceActivity
import com.kyrex.chat.ChatActivity

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class IntegratedModulesTest {
    private fun walk(view: View): List<View> = listOf(view) + if (view is ViewGroup) (0 until view.childCount).flatMap { walk(view.getChildAt(it)) } else emptyList()
    @Test fun voiceSetupHasNoServerOrPairingCodeInputsAndDoesNotStartListening() {
        val controller = Robolectric.buildActivity(VoiceActivity::class.java).create()
        val views = walk(controller.get().window.decorView)
        assertTrue(views.none { it is EditText })
        assertTrue(views.any { it is TextView && it.text.toString() == "Connect voice to Kyrex" })
        assertTrue(views.any { it is TextView && it.text.toString() == "Start listening" })
        assertFalse(com.kyrex.voice.VoiceService.active)
        controller.destroy()
    }
    @Test fun oneLauncherAndFeatureNotificationsOpenUnifiedSettings() {
        val context = RuntimeEnvironment.getApplication() as Context
        val launcher = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER).setPackage(context.packageName)
        val activities = context.packageManager.queryIntentActivities(launcher,0)
        assertEquals(1,activities.size)
        assertEquals(ChatActivity::class.java.name,activities.single().activityInfo.name)
        assertEquals(VoiceActivity::class.java.name,com.kyrex.voice.MainActivity.settingsIntent(context).component?.className)
        assertEquals(IntegratedMessagesActivity::class.java.name,MainActivity.settingsIntent(context).component?.className)
        assertTrue(context.assets.open("COPYING-AGPL.txt").bufferedReader().use { it.readText().contains("GNU AFFERO GENERAL PUBLIC LICENSE") })
    }
    @Test fun healthAndMessagesNeverShareTheirCredentialPreferences() {
        val context = RuntimeEnvironment.getApplication() as Context
        val messages = SessionStore(context)
        val name = ReflectionHelpers.getField<String>(messages,"preferences")
        assertNotEquals("pairing",name)
        context.getSharedPreferences(name,0).edit().putString("encrypted","saved-messages-session").commit()
        context.getSharedPreferences("pairing",0).edit().putString("token","saved-health-session").commit()
        context.getSharedPreferences("pairing",0).edit().clear().commit()
        assertEquals("saved-messages-session",context.getSharedPreferences(name,0).getString("encrypted",null))
        context.getSharedPreferences(name,0).edit().clear().commit()
    }
}
