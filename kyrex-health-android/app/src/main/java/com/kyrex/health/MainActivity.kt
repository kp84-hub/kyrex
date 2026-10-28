package com.kyrex.health

import android.os.Bundle
import android.widget.*
import androidx.activity.ComponentActivity
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.PermissionController
import androidx.health.connect.client.permission.HealthPermission
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.launch
import org.json.JSONObject

internal const val PRIVACY = "Kyrex Health reads only Samsung Health workouts, steps, heart rate, calories, distance and sleep you allow through Health Connect. Tap Sync to send the last 7 days to your paired Kyrex server. Records are encrypted on that server and pruned to a 90-day window during sync. This app never writes health data. Optional automatic sync uploads about once an hour when enabled with Android background read permission; Android may delay it for battery or connectivity. Turn automatic sync off here to stop scheduled uploads. Disconnect Samsung Health in Kyrex to revoke this phone and erase retained records. Remove permissions in Android settings to stop local access. Other Health Connect sources are excluded."

class PrivacyActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(TextView(this).apply { text = PRIVACY; setPadding(32, 48, 32, 32) })
    }
}

open class MainActivity : ComponentActivity() {
    protected open val integratedChat = false
    protected open fun startIntegratedConnection() {}
    protected open fun returnToChat() { finish() }
    protected open fun forgetIntegratedConnection() {}
    private val prefs by lazy { getSharedPreferences("pairing", MODE_PRIVATE) }
    private lateinit var server: EditText
    private lateinit var code: EditText
    private lateinit var status: TextView
    private lateinit var autoSync: Switch
    private var updatingSwitch = false
    private var requestingBackground = false
    private var backgroundPermissionResultPending = false
    private var autoSyncPairing: String? = null
    private val health by lazy { HealthSync(this) }
    private val permissions = HealthSync.permissions
    private val backgroundRequest = registerForActivityResult(PermissionController.createRequestPermissionResultContract()) {
        backgroundHealthPermissionReturned()
    }
    private val permissionRequest = registerForActivityResult(PermissionController.createRequestPermissionResultContract()) { granted ->
        status.text = "Granted ${granted.size} data permissions. Tap Sync when ready."
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        requestingBackground = savedInstanceState?.getBoolean("requesting_background", false) ?: false
        autoSyncPairing = savedInstanceState?.getString("background_pairing_revision")
        val layout = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(32, 32, 32, 32) }
        ViewCompat.setOnApplyWindowInsetsListener(layout) { view, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            view.setPadding(32 + bars.left, 32 + bars.top, 32 + bars.right, 32 + bars.bottom)
            insets
        }
        layout.addView(TextView(this).apply { text = if (integratedChat) "Samsung Health" else "Kyrex Health"; textSize = 24f })
        layout.addView(TextView(this).apply { text = if (integratedChat)
            "Choose which Samsung Health readings Kyrex can read through Health Connect. Sync shares the last seven days with your Kyrex account. Health data is never written back."
            else PRIVACY })
        server = EditText(this).apply { hint = "Kyrex server, https://…"; setText(prefs.getString("server", "")); inputType = android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_VARIATION_URI }
        code = EditText(this).apply { hint = "Pairing code from Kyrex Connections" }
        status = TextView(this)
        if (!integratedChat) { layout.addView(server); layout.addView(code) }
        fun button(label: String, action: () -> Unit) { layout.addView(Button(this).apply { text = label; setOnClickListener { action() } }) }
        if (integratedChat) {
            button("Back to Kyrex Chat") { returnToChat() }
            button("How health data is used") { startActivity(android.content.Intent(this, PrivacyActivity::class.java)) }
            button("Connect health to Kyrex") { startIntegratedConnection() }
        } else button("Pair with Kyrex") { runTask("Pairing") {
            val base = health.checkedServer(server.text.toString())
            val result = health.post(base, "/api/connections/samsung_health/exchange", JSONObject().put("pairing_code", code.text.toString().trim().uppercase()))
            val token = result.getString("device_token")
            prefs.edit().putString("server", base).putString("token", health.seal(token)).apply()
            code.setText(""); status.text = "Paired. Allow Health Connect access, then tap Sync."
        } }
        button("Allow Health Connect access") {
            if (healthAvailable(this)) permissionRequest.launch(permissions)
            else status.text = "Health Connect is unavailable. Install or update Health Connect and Samsung Health."
        }
        button("Sync last 7 days") { runTask("Sync last 7 days") {
            if (integratedChat && !prefs.contains("token")) {
                status.text = "Connect health to Kyrex first, then allow Health Connect access."
                return@runTask
            }
            val sent = health.sync(progress = { step -> currentAction = step; status.text = "$step…" })
            val skipped = prefs.getInt("last_skipped", 0)
            status.text = "Synced $sent Samsung Health readings." + if (skipped > 0) " Skipped $skipped readings with invalid timestamps; coverage is incomplete." else ""
        } }
        autoSync = Switch(this).apply {
            text = "Automatic sync (about once an hour)"
            isChecked = prefs.getBoolean("auto_sync", false)
            setOnCheckedChangeListener { _, checked ->
                if (updatingSwitch) return@setOnCheckedChangeListener
                if (taskRunning || requestingBackground) {
                    setAutoSwitch(prefs.getBoolean("auto_sync", false) || requestingBackground)
                    return@setOnCheckedChangeListener
                }
                if (checked) runTask("Enable automatic sync") { requestAutoSync() }
                else pauseAutoSync("Automatic sync off. Manual sync is available.")
            }
        }
        layout.addView(autoSync)
        button("Forget pairing on this phone") {
            if (integratedChat) forgetIntegratedConnection()
            requestingBackground = false; backgroundPermissionResultPending = false; autoSyncPairing = null
            HealthSyncWorker.disable(this); prefs.edit().clear().apply(); autoSync.isChecked = false; server.setText(""); status.text = "Local pairing removed. Disconnect in Kyrex Connections to revoke the server pairing and erase synced data."
        }
        layout.addView(status)
        setContentView(ScrollView(this).apply { addView(layout) })
        updateAutoSyncControls()
        showSyncStatus()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        outState.putBoolean("requesting_background", requestingBackground)
        outState.putString("background_pairing_revision", autoSyncPairing)
        super.onSaveInstanceState(outState)
    }

    override fun onResume() {
        super.onResume()
        if (::status.isInitialized) {
            // Permission callbacks can still be checking grants when onResume runs.
            // Do not replace their pending state with the previous saved opt-in.
            if (!taskRunning) {
                setAutoSwitch(prefs.getBoolean("auto_sync", false) || requestingBackground)
                showSyncStatus()
            }
            updateAutoSyncControls()
        }
    }

    private fun setAutoSwitch(enabled: Boolean) {
        updatingSwitch = true
        autoSync.isChecked = enabled
        updatingSwitch = false
    }

    private fun updateAutoSyncControls() {
        autoSync.isEnabled = !taskRunning && !requestingBackground
    }

    private fun saveSyncStatus(message: String) {
        prefs.edit().putString("sync_status", message).apply()
        showSyncStatus()
    }

    private fun pauseAutoSync(message: String) {
        requestingBackground = false; backgroundPermissionResultPending = false; autoSyncPairing = null
        HealthSyncWorker.disable(this)
        setAutoSwitch(false)
        saveSyncStatus(message)
    }

    // Keep Android permission access at the native boundary; tests substitute
    // controlled platform responses without uploading health data.
    protected open fun healthConnectAvailable() = healthAvailable(this)
    protected open fun backgroundHealthAvailable() =
        HealthSync.backgroundAvailable(HealthConnectClient.getOrCreate(this))
    protected open suspend fun grantedHealthPermissions(): Set<String> =
        HealthConnectClient.getOrCreate(this).permissionController.getGrantedPermissions()
    protected open fun launchBackgroundHealthPermission() {
        backgroundRequest.launch(setOf(HealthPermission.PERMISSION_READ_HEALTH_DATA_IN_BACKGROUND))
    }

    protected fun backgroundHealthPermissionReturned() {
        if (autoSyncPairing == null) return
        // Some permission contracts return synchronously. Do not let runTask's
        // busy guard discard that result while requestAutoSync is finishing.
        if (taskRunning) { backgroundPermissionResultPending = true; return }
        requestingBackground = false
        // Re-read actual permissions, including after activity recreation. The
        // callback's result is not permission to skip the normal grant checks.
        runTask("Enable automatic sync") { enableAutoSync() }
    }

    private fun showSyncStatus() {
        val last = prefs.getLong("last_sync", 0)
        status.text = if (last > 0) "Last successful sync: ${java.text.DateFormat.getDateTimeInstance().format(java.util.Date(last))}. ${prefs.getInt("last_count", 0)} readings. ${prefs.getString("sync_status", "")}" else prefs.getString("sync_status", "Pair, allow access, then sync.")
    }

    private suspend fun requestAutoSync() {
        autoSyncPairing = pairingRevision()
        val allowed = autoSyncPermissions() ?: return
        if (HealthPermission.PERMISSION_READ_HEALTH_DATA_IN_BACKGROUND in allowed) enableAutoSync()
        else {
            requestingBackground = true
            saveSyncStatus("Waiting for background Health Connect access. Automatic sync is not enabled yet.")
            updateAutoSyncControls()
            try { launchBackgroundHealthPermission() }
            catch (e: Exception) { requestingBackground = false; throw e }
        }
    }

    private suspend fun autoSyncPermissions(): Set<String>? {
        val reason = when {
            !prefs.contains("token") || !prefs.contains("server") -> "Pair with Kyrex before enabling automatic sync."
            !healthConnectAvailable() -> "Health Connect is unavailable. Install or update it before enabling automatic sync."
            !backgroundHealthAvailable() -> "Background health access is unavailable on this phone. Manual sync is available."
            else -> null
        }
        if (reason != null) { pauseAutoSync(reason); return null }
        val allowed = grantedHealthPermissions()
        if (autoSyncPairing == null || autoSyncPairing != pairingRevision()) {
            pauseAutoSync("Health connection changed. Enable automatic sync again for the current connection.")
            return null
        }
        if (allowed.intersect(permissions).isEmpty()) {
            pauseAutoSync("Allow Health Connect access first, then enable automatic sync again.")
            return null
        }
        return allowed
    }

    private fun pairingRevision(): String? {
        val sealed = prefs.getString("token", null) ?: return null
        val base = prefs.getString("server", null) ?: return null
        // Saved activity state carries only a digest, never the upload credential.
        val bytes = "$base\n$sealed".toByteArray(Charsets.UTF_8)
        return java.security.MessageDigest.getInstance("SHA-256").digest(bytes)
            .joinToString("") { "%02x".format(it) }
    }

    private suspend fun enableAutoSync() {
        val allowed = autoSyncPermissions() ?: return
        if (HealthPermission.PERMISSION_READ_HEALTH_DATA_IN_BACKGROUND !in allowed) {
            pauseAutoSync("Background access was not granted. Enable background health access in Health Connect, then enable automatic sync again. Manual sync is available.")
            return
        }
        prefs.edit().putBoolean("auto_sync", true).apply()
        try { HealthSyncWorker.schedule(this) }
        catch (e: Exception) { prefs.edit().putBoolean("auto_sync", false).apply(); throw e }
        setAutoSwitch(true)
        autoSyncPairing = null
        saveSyncStatus("Automatic sync enabled. Android schedules it about once an hour with internet access.")
    }

    private var currentAction = "Action"
    private var taskRunning = false

    protected fun completeIntegratedPairing(base: String, token: String, owner: String) {
        requestingBackground = false; backgroundPermissionResultPending = false; autoSyncPairing = null
        HealthSyncWorker.disable(this)
        prefs.edit().clear().putString("server", health.checkedServer(base))
            .putString("token", health.seal(token)).putString("owner", owner)
            .putBoolean("integrated", true).putBoolean("auto_sync", false)
            .putString("sync_status", "Connected to $owner. Allow Health Connect access, then sync.").apply()
        setAutoSwitch(false)
        showSyncStatus()
    }

    protected fun runTask(action: String, block: suspend () -> Unit) {
        if (taskRunning) return
        taskRunning = true
        updateAutoSyncControls()
        currentAction = action
        lifecycleScope.launch {
            status.text = "$action…"
            try { block() }
            catch (e: kotlinx.coroutines.CancellationException) { throw e }
            catch (e: Exception) {
                if (currentAction == "Enable automatic sync") {
                    requestingBackground = false; backgroundPermissionResultPending = false; autoSyncPairing = null
                }
                if (::autoSync.isInitialized) setAutoSwitch(prefs.getBoolean("auto_sync", false))
                val message = healthFailureMessage(currentAction, e)
                prefs.edit().putString("sync_status", message).apply()
                status.text = message
            } finally {
                taskRunning = false
                updateAutoSyncControls()
                if (backgroundPermissionResultPending) {
                    backgroundPermissionResultPending = false
                    backgroundHealthPermissionReturned()
                }
            }
        }
    }

}
