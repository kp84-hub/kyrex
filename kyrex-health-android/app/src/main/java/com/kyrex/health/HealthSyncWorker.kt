package com.kyrex.health

import android.content.Context
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.permission.HealthPermission
import androidx.work.*
import kotlinx.coroutines.CancellationException
import java.util.concurrent.TimeUnit

class HealthSyncWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        val health = HealthSync(applicationContext)
        val prefs = health.prefs
        if (!prefs.getBoolean("auto_sync", false)) return Result.success()
        fun pause(message: String): Result {
            prefs.edit().putBoolean("auto_sync", false).putString("sync_status", message).apply()
            return Result.success()
        }
        if (!prefs.contains("token")) return pause("Automatic sync paused: pair with Kyrex again.")
        if (HealthConnectClient.getSdkStatus(applicationContext) != HealthConnectClient.SDK_AVAILABLE)
            return pause("Automatic sync paused: Health Connect is unavailable.")
        try {
            val client = HealthConnectClient.getOrCreate(applicationContext)
            val granted = client.permissionController.getGrantedPermissions()
            if (!HealthSync.backgroundAvailable(client) || HealthPermission.PERMISSION_READ_HEALTH_DATA_IN_BACKGROUND !in granted ||
                granted.intersect(HealthSync.permissions).isEmpty())
                return pause("Automatic sync paused: restore health permissions and enable automatic sync again.")
            health.sync(background = true)
            return Result.success()
        } catch (e: CancellationException) {
            throw e
        } catch (_: SecurityException) {
            return pause("Automatic sync paused: restore health permissions and enable automatic sync again.")
        } catch (e: SyncHttpException) {
            if (e.statusCode == 401 || e.statusCode == 403)
                return pause("Automatic sync paused: pairing was revoked. Pair with Kyrex again.")
            if (e.statusCode in 400..499 && e.statusCode != 429) {
                prefs.edit().putString("sync_status", "Automatic sync could not upload. Open the app and try manual sync.").apply()
                return Result.failure()
            }
        } catch (_: Exception) {
            // Transient connectivity and provider quota failures use bounded WorkManager backoff.
        }
        prefs.edit().putString("sync_status", "Automatic sync delayed. Will retry with internet access.").apply()
        return if (runAttemptCount < 3) Result.retry() else Result.failure()
    }

    companion object {
        private const val WORK_NAME = "kyrex-health-auto-sync"
        fun schedule(context: Context) {
            val request = PeriodicWorkRequestBuilder<HealthSyncWorker>(1, TimeUnit.HOURS)
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).setRequiresBatteryNotLow(true).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 1, TimeUnit.MINUTES)
                .build()
            WorkManager.getInstance(context).enqueueUniquePeriodicWork(WORK_NAME, ExistingPeriodicWorkPolicy.KEEP, request)
        }
        fun disable(context: Context) {
            context.getSharedPreferences("pairing", Context.MODE_PRIVATE).edit().putBoolean("auto_sync", false).apply()
            WorkManager.getInstance(context).cancelUniqueWork(WORK_NAME)
        }
    }
}
