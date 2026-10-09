# Kyrex Health Android companion

Read-only Samsung Health → Health Connect → Kyrex bridge. Pair with a one-use
code from Kyrex Connections, grant the chosen read permissions, and sync the last seven days manually or enable automatic sync. See [setup and data behavior](../docs/connectors/fitness.md).

Open this directory in Android Studio, using JDK 17 and Android SDK 36. Build
with Gradle 8.11.1 (`gradle :app:assembleDebug`). No server credentials are compiled
into the app; the user supplies their own HTTPS Kyrex origin during pairing.

Version 0.2 adds optional **Automatic sync**. Pair and grant normal read access first, then enable the switch and grant Android's separate background health permission. WorkManager syncs approximately hourly when internet is available and battery is not low, survives normal app closure and phone restarts, and may be delayed by Android battery restrictions. Force-stopping the app prevents work until it is opened again. Devices without the Health Connect background feature retain manual sync. Disable the switch to cancel scheduled work; forgetting pairing also cancels it. Revoked health access or server pairing pauses automatic sync until permissions/pairing are restored and the switch is enabled again. The app displays the last successful sync and any subsequent failure. No health data or credentials enter WorkManager inputs, outputs or logs.

For repeatable debug updates, keep the signing keystore outside Git and set `KYREX_HEALTH_DEBUG_KEYSTORE` to its absolute path. Android requires the same signing key for an in-place update. A build with a different key requires uninstalling the old companion and pairing again; server-side health records remain. Never commit the signing key.

Version 0.4 skips Samsung records with timestamp intervals that the server would reject instead of failing the entire snapshot. The original timestamps are never rewritten. The app reports the skipped count; completed uploads also send that count so fitness reads can flag incomplete coverage. Skipped records are reconsidered on the next sync if the provider corrects them. Install over 0.2/0.3 using the retained signing key to preserve pairing.

Version 0.5 attaches available Samsung session heart-rate average/min/peak/count,
active and total calories, distance and steps to each workout. It requests the
additional read permissions and aggregates only Samsung data within the session
interval. Optional metric failures do not discard the session or other metrics.
Grant the new permissions and sync the last seven days after updating. The
backend must also be updated to retain and expose these fields.
