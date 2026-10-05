# Fitness connectors and Workout Bot

## Oura (read-only cloud connector)

Register a server-side OAuth application at https://developer.ouraring.com/applications.
Use the published [fitness privacy policy](fitness-privacy.md) and [fitness terms](fitness-terms.md) for the application registration, and select only Daily, Heartrate, and Workout scopes.
Configure its allowed redirect to your Kyrex host's exact HTTPS URL:
`https://YOUR_KYREX_HOST/api/connections/oura/callback`.
Set these host environment variables (never commit their values):

- `KYREX_OURA_CLIENT_ID`
- `KYREX_OURA_CLIENT_SECRET`
- `KYREX_OURA_REDIRECT_URI` (the same exact callback URL)

Reuse the existing connector encryption key (`WEB_SESSION_SECRET` or the
connector secret key configured by `connectors._box`). Keep that key stable
across deployments. Persist `KYREX_DATA_DIR` on a host volume.

In Chat, open Connections → Oura Ring → Connect and approve the data scopes
you want to share. Kyrex requests only `daily`, `workout`, and `heartrate`.
It uses single-use owner-bound OAuth state, PKCE, encrypted token storage,
and serialized refresh rotation. Missing permissions and provider failures
are reported separately from empty results. Token values never enter Chat or
engine sessions. Disconnect removes credentials and invalidates pending OAuth.
Oura application registration and the owner's consent are required for live
access; code deployment alone cannot create them.

## Workout Bot

In Bots, choose **Create Workout Bot**, configure its own provider/model if
needed, create it, and press Start. The template grants only `fitness:read`,
`cal:list`, and `glofox:read`; it does not grant browser access, editing, calendar
writes, shell execution, or delegation. Its model selection uses the existing
owner-scoped Bot provider settings. It reads actual connected wearable data
through the engine's host-mediated `fitness_read` tool.

Example: "Compare my sleep, recovery and completed workouts for the last week."
Use explicit dates for comparisons. Fitness ranges use **UTC date boundaries**
for Health Connect records and Oura's provider-assigned `day` for daily scores.
The tool defaults to the last seven UTC dates and limits each range to 31 days.
Heart rate is a separate collection so normal summaries do not fetch thousands
of samples. Bounded provider pagination and record caps return `truncated`.
Cross-device workouts with at least 80% overlap of the longer session are marked
`possible_duplicate_of`; both source records remain visible, and the bot is
instructed not to count the marked Oura session a second time.

This first slice supports direct Workout Bot chats. Jev recognizes the workout
persona in routing metadata. Generic durable `delegate_task` fitness execution
is not introduced by this change; use the Workout Bot chat for wearable analysis.

## Samsung Health / Galaxy Watch (Android companion)

Samsung watch data flows to Samsung Health on the phone, then Health Connect.
The web chat cannot call the device-local Health Connect API. The companion
source lives in `kyrex-health-android`; build with Android Studio (JDK 17, SDK 36,
Gradle 8.11.1) or `gradle :app:assembleDebug`. Health Connect dependency: stable
`androidx.health.connect:connect-client:1.1.0`. The APK is at
`app/build/outputs/apk/debug/app-debug.apk` after a successful build.

1. Install the companion on your Android phone.
2. In Samsung Health, enable Health Connect sharing for steps, exercise, sleep,
   and heart rate. Wait for the watch to sync to Samsung Health.
3. In Kyrex Connections → Samsung Health, tap **Pair phone**.
4. Enter your Kyrex HTTPS server origin and the pairing code in the companion.
   Verify the server is your Kyrex host before pairing.
5. Tap **Allow Health Connect access**, grant the desired read permissions, and
   tap **Sync last 7 days**. Manual sync is available; companion 0.2 also offers optional hourly automatic sync.

Only records whose Health Connect origin is `com.sec.android.app.shealth` are
uploaded. Oura and other Health Connect writers are excluded. Sleep duration is
computed from sleeping/light/deep/REM stages; sessions without staging are
skipped instead of treating time in bed as actual sleep. Repeated uploads
upsert stable origin/id pairs. Last-sync time is set only after the phone reports
a complete sync; failed partial batches never claim a completed snapshot. Local device credentials are encrypted with an
Android Keystore key; the app disables backups and cleartext traffic. It never
writes Health Connect records. Background uploads require enabling automatic sync and granting Android background health access.

Phone pairing codes are high entropy, expire after ten minutes, and can be used
once. The resulting token can only ingest health records; it cannot read owner
health data or use other Kyrex APIs. The server derives the owner from the token,
never from the uploaded body. Credentials and measurements are encrypted on the
host; only hashed record identifiers and timestamps are indexed. Records are
pruned to a 90-day window on successful uploads; cleanup is not a background
job, so an idle disconnected phone does not trigger retention pruning. Disconnect Samsung
Health revokes the phone token, invalidates pairing codes, and deletes records.
Re-pairing replaces the device token. Local Forget pairing alone does not revoke
server access; use Disconnect in Kyrex for that.

The initial companion upserts the last seven days; it does not yet mirror
provider-side deletions or permission revocations into historical server data.
Previously synced readings remain until Kyrex Disconnect or retention pruning.
Wearable synchronization follows each provider's timing, so this is not a
real-time watch feed. Play Store publication is a separate step and requires
Google's Health Connect declarations; none is performed by this PR.

## Verification

Automated tests use fake Oura transport and synthetic fitness records; no live
health account is accessed. They exercise OAuth state replay/expiry, refresh,
owner isolation, missing scopes, provider failure, pagination, phone token scope
and revocation, atomic upload validation, duplicate detection, host-tool grants,
Workout Bot creation/provider isolation, consent URLs and secret-free UI cards.
Live Oura consent and an on-device Samsung Health sync remain acceptance checks.

Sources:
- https://cloud.ouraring.com/docs/authentication
- https://developer.samsung.com/health/health-connect-faq.html
- https://developer.android.com/jetpack/androidx/releases/health-connect

### Optional Android automatic sync (companion 0.2)

After pairing and granting read access, enable **Automatic sync** and approve the separate background health permission. The companion schedules an approximately hourly sync through Android WorkManager when internet is available and the battery is not low. Android can delay runs; force-stop pauses them until the app is opened. Unsupported devices retain manual sync. Last successful sync and retry/pause status are shown in the app. Turning the switch off or forgetting local pairing cancels scheduled work. Revoked permissions or pairing pause it and require restoring access and enabling the switch again. Background and manual runs share the same encrypted credentials, Samsung-only filter, seven-day read window, serialized sync and server upserts.

The Android companion skips records with invalid timestamp intervals before uploading valid readings. A completed snapshot reports `skipped_records`; Samsung fitness reads expose this count and `incomplete=true` when any records were skipped. Treat those snapshots as partial coverage. Subsequent snapshots can clear the warning when all readings pass validation. No record timestamp is rewritten to make it pass validation.
