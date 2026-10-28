package com.kyrex.health

import android.os.Looper
import android.view.View
import android.view.ViewGroup
import android.widget.Switch
import android.widget.TextView
import androidx.health.connect.client.permission.HealthPermission
import androidx.work.Configuration
import androidx.work.WorkManager
import androidx.work.testing.WorkManagerTestInitHelper
import kotlinx.coroutines.CompletableDeferred
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

// Controlled permission-provider boundary. No health reads, credentials or
// uploads; the real Activity, switch, lifecycle and WorkManager are exercised.
class AutoSyncTestActivity : MainActivity() {
    var available = true
    var backgroundAvailable = true
    var granted = HealthSync.permissions
    var permissionLaunches = 0
    var replyImmediately = false
    var grantOnReply = false
    var permissionGate: CompletableDeferred<Unit>? = null
    override fun healthConnectAvailable() = available
    override fun backgroundHealthAvailable() = backgroundAvailable
    override suspend fun grantedHealthPermissions(): Set<String> {
        permissionGate?.await()
        return granted
    }
    override fun launchBackgroundHealthPermission() {
        permissionLaunches++
        if (grantOnReply) granted = granted + HealthPermission.PERMISSION_READ_HEALTH_DATA_IN_BACKGROUND
        if (replyImmediately) backgroundHealthPermissionReturned()
    }
    fun permissionReturned() = backgroundHealthPermissionReturned()
    fun busyUntil(gate: CompletableDeferred<Unit>) = runTask("Sync in progress") { gate.await() }
}

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class HealthAutoSyncActivityTest {
    private val context get() = RuntimeEnvironment.getApplication()
    private val prefs get() = context.getSharedPreferences("pairing", 0)
    private val manager get() = WorkManager.getInstance(context)
    private val background = HealthPermission.PERMISSION_READ_HEALTH_DATA_IN_BACKGROUND
    @Before fun prepare() {
        prefs.edit().clear().putString("server", "https://example.test")
            .putString("token", "inert-pairing-fixture").commit()
        WorkManagerTestInitHelper.initializeTestWorkManager(context, Configuration.Builder().build())
    }
    private fun walk(view: View): List<View> = listOf(view) + if (view is ViewGroup)
        (0 until view.childCount).flatMap { walk(view.getChildAt(it)) } else emptyList()
    private fun toggle(activity: MainActivity) = walk(activity.window.decorView).filterIsInstance<Switch>().single()
    private fun statusContains(activity: MainActivity, text: String) = walk(activity.window.decorView)
        .filterIsInstance<TextView>().any { it.text.toString().contains(text) }
    private fun idle() = shadowOf(Looper.getMainLooper()).idle()
    private fun activeJobs() = manager.getWorkInfosForUniqueWork("kyrex-health-auto-sync").get()
        .filter { !it.state.isFinished }

    @Test fun deniedPermissionReasonSurvivesResumeAndRecreation() {
        val controller = Robolectric.buildActivity(AutoSyncTestActivity::class.java).setup()
        val activity = controller.get()
        toggle(activity).isChecked = true; idle()
        assertEquals(1, activity.permissionLaunches)
        assertFalse(prefs.getBoolean("auto_sync", false))
        activity.permissionReturned(); idle()
        assertFalse(toggle(activity).isChecked)
        assertTrue(toggle(activity).isEnabled)
        assertTrue(statusContains(activity, "Background access was not granted"))
        controller.pause().resume(); idle()
        assertTrue(statusContains(controller.get(), "Background access was not granted"))
        controller.recreate(); idle()
        assertTrue(statusContains(controller.get(), "Background access was not granted"))
        assertFalse(toggle(controller.get()).isChecked)
        assertTrue(activeJobs().isEmpty())
        controller.pause().stop().destroy()
    }

    @Test fun synchronousPermissionResultIsNotDroppedByBusyGuard() {
        val controller = Robolectric.buildActivity(AutoSyncTestActivity::class.java).setup()
        val activity = controller.get()
        activity.replyImmediately = true; activity.grantOnReply = true
        toggle(activity).isChecked = true; idle()
        assertTrue(prefs.getBoolean("auto_sync", false))
        assertTrue(toggle(activity).isChecked)
        assertTrue(toggle(activity).isEnabled)
        assertEquals(1, activeJobs().size)
        assertTrue(statusContains(activity, "Automatic sync enabled"))
        controller.pause().resume(); idle()
        assertTrue(toggle(activity).isChecked)
        controller.pause().stop().destroy()
    }

    @Test fun resumeWhileCheckingGrantedPermissionDoesNotTurnSwitchOff() {
        val controller = Robolectric.buildActivity(AutoSyncTestActivity::class.java).setup()
        val activity = controller.get()
        activity.granted = activity.granted + background
        val gate = CompletableDeferred<Unit>(); activity.permissionGate = gate
        toggle(activity).isChecked = true; idle()
        assertFalse(toggle(activity).isEnabled)
        controller.pause().resume(); idle()
        assertTrue(toggle(activity).isChecked)
        gate.complete(Unit); idle()
        assertTrue(prefs.getBoolean("auto_sync", false))
        assertTrue(toggle(activity).isChecked)
        assertEquals(1, activeJobs().size)
        controller.pause().stop().destroy()
    }

    @Test fun pendingPermissionRequestSurvivesRecreationAndRechecksCurrentGrants() {
        val controller = Robolectric.buildActivity(AutoSyncTestActivity::class.java).setup()
        toggle(controller.get()).isChecked = true; idle()
        controller.recreate(); idle()
        val activity = controller.get()
        assertTrue(toggle(activity).isChecked)
        assertFalse(toggle(activity).isEnabled)
        assertFalse(prefs.getBoolean("auto_sync", false))
        activity.granted = activity.granted + background
        activity.permissionReturned(); idle()
        assertTrue(prefs.getBoolean("auto_sync", false))
        assertTrue(toggle(activity).isEnabled)
        assertEquals(1, activeJobs().size)
        toggle(activity).isChecked = false; idle()
        assertFalse(prefs.getBoolean("auto_sync", true))
        assertTrue(activeJobs().isEmpty())
        controller.pause().resume(); idle()
        assertTrue(statusContains(activity, "Automatic sync off"))
        controller.pause().stop().destroy()
    }

    @Test fun busyOperationCannotAcceptAndThenDiscardAnEnableTap() {
        val controller = Robolectric.buildActivity(AutoSyncTestActivity::class.java).setup()
        val activity = controller.get()
        val gate = CompletableDeferred<Unit>(); activity.busyUntil(gate); idle()
        assertFalse(toggle(activity).isEnabled)
        // Even an accessibility/programmatic checked event cannot bypass busy state.
        toggle(activity).isChecked = true; idle()
        assertFalse(toggle(activity).isChecked)
        assertFalse(prefs.getBoolean("auto_sync", false))
        assertEquals(0, activity.permissionLaunches)
        gate.complete(Unit); idle()
        assertTrue(toggle(activity).isEnabled)
        controller.pause().stop().destroy()
    }

    @Test fun delayedPermissionCallbackCannotEnableSyncForAReplacementPairing() {
        val controller = Robolectric.buildActivity(AutoSyncTestActivity::class.java).setup()
        val activity = controller.get()
        toggle(activity).isChecked = true; idle()
        prefs.edit().putString("token", "different-inert-pairing-fixture").commit()
        activity.granted = activity.granted + background
        activity.permissionReturned(); idle()
        assertFalse(prefs.getBoolean("auto_sync", false))
        assertFalse(toggle(activity).isChecked)
        assertTrue(statusContains(activity, "Health connection changed"))
        assertTrue(activeJobs().isEmpty())
        controller.pause().stop().destroy()
    }

    @Test fun backgroundGrantWithoutAnyCurrentDataGrantCannotEnableUploads() {
        val controller = Robolectric.buildActivity(AutoSyncTestActivity::class.java).setup()
        val activity = controller.get()
        toggle(activity).isChecked = true; idle()
        activity.granted = setOf(background)
        activity.permissionReturned(); idle()
        assertFalse(prefs.getBoolean("auto_sync", false))
        assertFalse(toggle(activity).isChecked)
        assertTrue(statusContains(activity, "Allow Health Connect access first"))
        assertTrue(activeJobs().isEmpty())
        controller.pause().stop().destroy()
    }

    @Test fun missingPairingProviderFeatureAndReadPermissionsHavePersistentReasons() {
        for ((blocked, reason) in listOf(
            "pairing" to "Pair with Kyrex",
            "provider" to "Health Connect is unavailable",
            "feature" to "Background health access is unavailable",
            "reads" to "Allow Health Connect access first")) {
            prefs.edit().putString("token", "inert-pairing-fixture").remove("sync_status").commit()
            val controller = Robolectric.buildActivity(AutoSyncTestActivity::class.java).setup()
            val activity = controller.get()
            when (blocked) {
                "pairing" -> prefs.edit().remove("token").commit()
                "provider" -> activity.available = false
                "feature" -> activity.backgroundAvailable = false
                "reads" -> activity.granted = setOf(background)
            }
            toggle(activity).isChecked = true; idle()
            assertFalse(blocked, toggle(activity).isChecked)
            assertFalse(blocked, prefs.getBoolean("auto_sync", false))
            controller.pause().resume(); idle()
            assertTrue(blocked, statusContains(activity, reason))
            assertTrue(blocked, activeJobs().isEmpty())
            controller.pause().stop().destroy()
        }
    }
}
