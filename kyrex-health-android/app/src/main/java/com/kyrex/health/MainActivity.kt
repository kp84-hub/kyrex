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

class MainActivity : ComponentActivity() {
    private val prefs by lazy { getSharedPreferences("pairing", MODE_PRIVATE) }
    private lateinit var server: EditText
    private lateinit var code: EditText
    private lateinit var status: TextView
    private lateinit var autoSync: Switch
    private var updatingSwitch = false
    private var requestingBackground = false
    private val health by lazy { HealthSync(this) }
    private val permissions = HealthSync.permissions
    private val backgroundRequest = registerForActivityResult(PermissionController.createRequestPermissionResultContract()) { granted ->
        requestingBackground = false
        if (HealthPermission.PERMISSION_READ_HEALTH_DATA_IN_BACKGROUND in granted) runTask("Enable automatic sync") { enableAutoSync() }
        else { autoSync.isChecked = false; status.text = "Background access was not granted. Manual sync is available." }
    }
    private val permissionRequest = registerForActivityResult(PermissionController.createRequestPermissionResultContract()) { granted ->
        status.text = "Granted ${granted.size} data permissions. Tap Sync when ready."
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val layout = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(32, 32, 32, 32) }
        ViewCompat.setOnApplyWindowInsetsListener(layout) { view, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            view.setPadding(32 + bars.left, 32 + bars.top, 32 + bars.right, 32 + bars.bottom)
            insets
        }
        layout.addView(TextView(this).apply { text = "Kyrex Health"; textSize = 24f })
        layout.addView(TextView(this).apply { text = PRIVACY })
        server = EditText(this).apply { hint = "Kyrex server, https://…"; setText(prefs.getString("server", "")); inputType = android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_VARIATION_URI }
        code = EditText(this).apply { hint = "Pairing code from Kyrex Connections" }
        status = TextView(this)
        layout.addView(server); layout.addView(code)
        fun button(label: String, action: () -> Unit) { layout.addView(Button(this).apply { text = label; setOnClickListener { action() } }) }
        button("Pair with Kyrex") { runTask("Pairing") {
            val base = health.checkedServer(server.text.toString())
            val result = health.post(base, "/api/connections/samsung_health/exchange", JSONObject().put("pairing_code", code.text.toString().trim().uppercase()))
            val token = result.getString("device_token")
            prefs.edit().putString("server", base).putString("token", health.seal(token)).apply()
            code.setText(""); status.text = "Paired. Allow Health Connect access, then tap Sync."
        } }
        button("Allow Health Connect access") {
            if (HealthConnectClient.getSdkStatus(this) == HealthConnectClient.SDK_AVAILABLE) permissionRequest.launch(permissions)
            else status.text = "Health Connect is unavailable. Install or update Health Connect and Samsung Health."
        }
        button("Sync last 7 days") { runTask("Sync last 7 days") {
            val sent = health.sync(progress = { step -> currentAction = step; status.text = "$step…" })
            val skipped = prefs.getInt("last_skipped", 0)
            status.text = "Synced $sent Samsung Health readings." + if (skipped > 0) " Skipped $skipped readings with invalid timestamps; coverage is incomplete." else ""
        } }
        autoSync = Switch(this).apply {
            text = "Automatic sync (about once an hour)"
            isChecked = prefs.getBoolean("auto_sync", false)
            setOnCheckedChangeListener { _, checked ->
                if (updatingSwitch) return@setOnCheckedChangeListener
                if (checked) runTask("Enable automatic sync") { requestAutoSync() }
                else { HealthSyncWorker.disable(this@MainActivity); status.text = "Automatic sync off. Manual sync is available." }
            }
        }
        layout.addView(autoSync)
        button("Forget pairing on this phone") {
            HealthSyncWorker.disable(this); prefs.edit().clear().apply(); autoSync.isChecked = false; server.setText(""); status.text = "Local pairing removed. Disconnect in Kyrex Connections to revoke the server pairing and erase synced data."
        }
        layout.addView(status)
        setContentView(ScrollView(this).apply { addView(layout) })
        showSyncStatus()
    }

    override fun onResume() {
        super.onResume()
        if (::status.isInitialized) {
            if (!requestingBackground) setAutoSwitch(prefs.getBoolean("auto_sync", false))
            showSyncStatus()
        }
    }

    private fun setAutoSwitch(enabled: Boolean) {
        updatingSwitch = true
        autoSync.isChecked = enabled
        updatingSwitch = false
    }

    private fun showSyncStatus() {
        val last = prefs.getLong("last_sync", 0)
        status.text = if (last > 0) "Last successful sync: ${java.text.DateFormat.getDateTimeInstance().format(java.util.Date(last))}. ${prefs.getInt("last_count", 0)} readings. ${prefs.getString("sync_status", "")}" else prefs.getString("sync_status", "Pair, allow access, then sync.")
    }

    private suspend fun requestAutoSync() {
        if (!prefs.contains("token")) { autoSync.isChecked = false; status.text = "Pair with Kyrex before enabling automatic sync."; return }
        if (HealthConnectClient.getSdkStatus(this) != HealthConnectClient.SDK_AVAILABLE) {
            autoSync.isChecked = false; status.text = "Health Connect is unavailable."; return
        }
        val client = HealthConnectClient.getOrCreate(this)
        if (!HealthSync.backgroundAvailable(client)) {
            autoSync.isChecked = false; status.text = "Background health access is unavailable on this phone. Manual sync is available."; return
        }
        val allowed = client.permissionController.getGrantedPermissions()
        if (allowed.intersect(permissions).isEmpty()) {
            autoSync.isChecked = false; status.text = "Allow Health Connect access first."; return
        }
        if (HealthPermission.PERMISSION_READ_HEALTH_DATA_IN_BACKGROUND in allowed) enableAutoSync()
        else { requestingBackground = true; backgroundRequest.launch(setOf(HealthPermission.PERMISSION_READ_HEALTH_DATA_IN_BACKGROUND)) }
    }

    private suspend fun enableAutoSync() {
        val client = HealthConnectClient.getOrCreate(this)
        val allowed = client.permissionController.getGrantedPermissions()
        if (!prefs.contains("token") || !HealthSync.backgroundAvailable(client) ||
            HealthPermission.PERMISSION_READ_HEALTH_DATA_IN_BACKGROUND !in allowed || allowed.intersect(permissions).isEmpty()) {
            autoSync.isChecked = false; status.text = "Pairing and health access are required."; return
        }
        prefs.edit().putBoolean("auto_sync", true).apply()
        setAutoSwitch(true)
        HealthSyncWorker.schedule(this)
        status.text = "Automatic sync enabled. Android schedules it about once an hour with internet access."
    }

    private var currentAction = "Action"
    private var taskRunning = false

    private fun runTask(action: String, block: suspend () -> Unit) {
        if (taskRunning) return
        taskRunning = true
        currentAction = action
        lifecycleScope.launch {
            status.text = "$action…"
            try { block() }
            catch (e: kotlinx.coroutines.CancellationException) { throw e }
            catch (e: Exception) {
                if (::autoSync.isInitialized) setAutoSwitch(prefs.getBoolean("auto_sync", false))
                val message = healthFailureMessage(currentAction, e)
                prefs.edit().putString("sync_status", message).apply()
                status.text = message
            } finally { taskRunning = false }
        }
    }

}
