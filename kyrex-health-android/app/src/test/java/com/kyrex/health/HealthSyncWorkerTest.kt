package com.kyrex.health

import android.content.Context
import androidx.work.*
import androidx.work.testing.TestListenableWorkerBuilder
import androidx.work.testing.WorkManagerTestInitHelper
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class HealthSyncWorkerTest {
    private lateinit var context: Context
    @Before fun prepare() {
        context = RuntimeEnvironment.getApplication()
        context.getSharedPreferences("pairing", Context.MODE_PRIVATE).edit().clear().commit()
        WorkManagerTestInitHelper.initializeTestWorkManager(context, Configuration.Builder().build())
    }
    @Test fun repeatedEnableCreatesOneNetworkConstrainedJobAndDisableCancelsIt() {
        val prefs = context.getSharedPreferences("pairing", Context.MODE_PRIVATE)
        prefs.edit().putBoolean("auto_sync", true).commit()
        HealthSyncWorker.schedule(context)
        HealthSyncWorker.schedule(context)
        val manager = WorkManager.getInstance(context)
        val jobs = manager.getWorkInfosForUniqueWork("kyrex-health-auto-sync").get()
        assertEquals(1, jobs.size)
        assertEquals(NetworkType.CONNECTED, jobs.single().constraints.requiredNetworkType)
        assertTrue(jobs.single().constraints.requiresBatteryNotLow())
        HealthSyncWorker.disable(context)
        assertFalse(prefs.getBoolean("auto_sync", true))
        assertEquals(WorkInfo.State.CANCELLED, manager.getWorkInfosForUniqueWork("kyrex-health-auto-sync").get().single().state)
    }
    @Test fun workerWithAutomaticSyncOffDoesNotAttemptToDecryptCredentials() = runBlocking {
        val prefs = context.getSharedPreferences("pairing", Context.MODE_PRIVATE)
        prefs.edit().putString("token", "invalid encrypted token").putBoolean("auto_sync", false).commit()
        val worker = TestListenableWorkerBuilder<HealthSyncWorker>(context).build()
        assertEquals(ListenableWorker.Result.success(), worker.doWork())
        assertFalse(prefs.contains("last_sync"))
    }
    @Test fun workerPausesWhenPairingHasBeenRemoved() = runBlocking {
        val prefs = context.getSharedPreferences("pairing", Context.MODE_PRIVATE)
        prefs.edit().putBoolean("auto_sync", true).commit()
        val worker = TestListenableWorkerBuilder<HealthSyncWorker>(context).build()
        assertEquals(ListenableWorker.Result.success(), worker.doWork())
        assertFalse(prefs.getBoolean("auto_sync", true))
        assertTrue(prefs.getString("sync_status", "")!!.contains("pair with Kyrex"))
        assertFalse(prefs.contains("last_sync"))
    }
}
