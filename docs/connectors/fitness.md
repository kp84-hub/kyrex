# Fitness connectors and Workout Bot

## Oura (read-only cloud connector)

Register a server-side OAuth application at https://cloud.ouraring.com/oauth/applications.
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
Use explicit dates for comparisons. In Chat, `fitness_read` accepts an IANA
`timezone` (default `America/New_York`) and uses its local midnight boundaries
for phone records, including daylight saving changes. For "today", the Bot
requests that single local date and the workout collection; without explicit
dates the tool defaults to seven local dates. Oura daily scores retain the
provider-assigned `day`. The direct HTTP read API retains its UTC default.
Each range is limited to 31 days. Raw heart rate remains a separate collection,
so normal summaries do not return thousands of samples. Workout records now
include `session_metrics` and per-metric availability, plus local timestamps.
Bounded provider pagination and record caps return `truncated`.
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
   heart rate, calories and distance as desired. Wait for the watch to sync to Samsung Health.
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

### Workout details (companion 0.5)

An exercise session supplies its interval, type and optional title; related
measurements are separate Health Connect records. Companion 0.5 requests
read-only active calories, total calories and distance access in addition to
the existing permissions. It aggregates each permitted metric over the exact
session interval with the Samsung origin filter: average/minimum/maximum heart
rate, sample count, active/total kcal, distance in meters and steps. Optional
read failures are isolated so a failed distance read preserves available heart
rate and the session. Missing permissions, empty data and read failures remain
distinct; absent values never become zero. Type 0 is labeled "Other workout".

After installing 0.5, tap **Allow Health Connect access** to grant the additional
permissions, check Samsung's sharing settings, then **Sync last 7 days** to
refresh existing sessions. Deploy the backend change as well. Existing Bot
chats receive current workout guidance without recreating the Bot or changing
its selected model. Replies lead with a concise local-time summary and available
measurements, omitting raw IDs and package names unless requested.

Older companion uploads are supported: the backend derives observed average,
minimum, peak and sample count from synced instantaneous HR samples within
`[session start, session end)`, using the same owner and origin. Duplicate
timestamp/BPM samples are collapsed. Phone session aggregates take precedence.
The fallback scans at most 20,000 samples and flags partial coverage if capped;
it is an average of observed samples, not an inferred whole-session intensity.
It cannot recover calories or distance that the old companion never uploaded.
Workout type, sets, reps and weights remain limited to what the provider exports;
the Bot must not substitute a planned Level 6 session as a measured activity.

Reference: https://developer.android.com/health-and-fitness/health-connect/experiences/workouts

### Native workout chart in Chat

Explicit requests to graph, chart or plot a workout trigger a fresh owner-scoped
host read before the model responds, so the card is delivered even when the
model could answer from older conversation history. Simple today/yesterday
requests use the current local date; no workout for that date means no session,
not a fallback to another day. Explicit dates and comparisons retain their
requested ranges. Both Cloud Dockerfiles build the current Chat UI in the image.

Successful Samsung workout reads attach a native report card to the assistant
reply. It plots up to 240 observed sample means with their min/max range and
breaks the line at sampling gaps over 90 seconds. The timeline is queried with
the same owner, Samsung origin and session boundaries as the measurements;
it is available even when phone session aggregates supply the summary values.
An average/peak without synced samples produces a summary card with no invented
curve. Query and curve limits are explicit. An uncapped query does not prove
continuous or complete recording.

The card includes duration, average/peak heart rate, active/total calories,
distance and steps when available. Tap a metric for its explanation and the
specific reason a missing value is unavailable. The LLM explains the observed
pattern briefly; the UI renders data, never executable model-generated chart
code. Reports are streamed and stored with the owner-scoped assistant reply
and remain visible when the conversation is reopened.

If a saved workout says `not_synced`, it lacks the newer detail payload. This
can happen when the phone syncs before the backend deployment, or an older
companion is still installed. With 0.5 installed, grant the desired access and
sync seven days again after deployment; no re-pair is needed. A fresh 0.5 upload
replaces the legacy record and distinguishes missing permission, no shared
data and read failure. This does not manufacture calories that Samsung never
exported to Health Connect.

Sources:
- https://cloud.ouraring.com/docs/authentication
- https://developer.samsung.com/health/health-connect-faq.html
- https://developer.android.com/jetpack/androidx/releases/health-connect

### Optional Android automatic sync (companion 0.2)

After pairing and granting read access, enable **Automatic sync** and approve the separate background health permission. The companion schedules an approximately hourly sync through Android WorkManager when internet is available and the battery is not low. Android can delay runs; force-stop pauses them until the app is opened. Unsupported devices retain manual sync. Last successful sync and retry/pause status are shown in the app. Turning the switch off or forgetting local pairing cancels scheduled work. Revoked permissions or pairing pause it and require restoring access and enabling the switch again. Background and manual runs share the same encrypted credentials, Samsung-only filter, seven-day read window, serialized sync and server upserts.

The Android companion skips records with invalid timestamp intervals before uploading valid readings. A completed snapshot reports `skipped_records`; Samsung fitness reads expose this count and `incomplete=true` when any records were skipped. Treat those snapshots as partial coverage. Subsequent snapshots can clear the warning when all readings pass validation. No record timestamp is rewritten to make it pass validation.

## Personal workout coaching

Profile setup happens in the **Workout Bot conversation**. On a personalized
review with missing context, the bot asks one short question at a time for age
(adult whole years), height, weight with units and goal; every field is optional.
A complete natural reply also works: “I am 42, 5 ft 10 in, 210 lb, and want better
endurance.” Later messages such as “My weight is now 205 lb” update that field
without replacing age, height or goal. “Forget my fitness profile” clears it.
A bare number is accepted only after the preceding assistant question identifies
one field and, for weight, one explicit unit. Ambiguous replies need clarification.

Compound goals are retained in encrypted `goal_details`, including the owner's
wording, weight targets and secondary aims. For example, “My goal is to lose
15lbs and be under 200lbs, but also build muscle and be lean” saves that context
alongside the primary category; the owner does not have to discard either aim.
The host retains explicit goal statements even when the model submits only
metrics or the legacy `goal` field. A short primary-category choice preserves
the earlier details; forgetting the goal removes both fields. Close spellings
such as “Weight managment” map to weight management. Target weights and pounds
to lose never become the current body weight.

Validation failures return `status: rejected` and `retryable: false`, which the
engine preserves for the model. The bot is instructed to ask a brief clarification
rather than repeat the rejected write. Storage failures remain unavailable or
unconfirmed; neither outcome is reported as an empty or successfully saved goal.

The `fitness_profile` Chat tool supports get/update/clear. It uses the existing
Firebase Firestore setup from chat memory (`KYREX_FIRESTORE_PROJECT_ID` and
`KYREX_FIRESTORE_SERVICE_ACCOUNT_JSON`); no additional Firebase project, browser
credentials or SDK is needed. Profiles live in `kyrex_fitness_profiles/{hashed
owner}`. Field values are sealed with the host connector key before upload.
Flat field merges preserve other fields atomically. Reads and writes disable
SDK retries and use four-second RPC timeouts; the Chat host additionally bounds
profile operations and keeps Stop responsive. A failed or timed-out mutation is
reported as unconfirmed, with a read advised before retrying. There is no SQLite
profile fallback or Settings form.

Both fitness tools use the existing owner fitness capability. Profile changes
are narrowly limited to facts supported by the current authenticated owner
message (or a short answer to the actual preceding question). Model-supplied
owner IDs, inferred values, ambiguous units and third-party facts cannot edit
profiles. This grant does not write wearable records, files, credentials or
other owners' preferences. Phone ingestion tokens have no profile route.
Clearing removes the Firestore profile; earlier chat messages and already-sent
provider requests are not retroactively erased. The general saved-memory toggle
controls general memories; fitness profiles are explicit Workout Bot context.

The host loads the current profile for each fitness Bot turn, including a new
conversation, so setup and later changes do not depend on transcript recall.
Workout/summary tool results also include `fitness_profile` and its availability
status. A Firebase outage or missing configuration is distinguished from an
empty profile and does not prevent wearable reads or native chart delivery.
Only fitness-granted Bots receive profiles; connection listings and unrelated
Bots do not. Single-workout chart/review requests still get fresh wearable data.
Complex date comparisons remain model-driven. The native graph and metric
explanations stay in place.

Coaching defaults to **What went well / Where to improve / Next workout**,
aligned with the saved goal and supported by observed data. Without a profile,
it gives general feedback and suggests setup. Usual activity is context, not a
confirmed session type. Low steps do not establish activity type, and HR does
not establish lifting technique, muscle growth or progress from one session.
Unknown recording phases cannot establish a missing warm-up or cool-down.
Height/weight alone do not establish fitness; the prompt does not introduce BMI,
calorie targets or weight-loss predictions unless requested. Wearable energy
values remain estimates, with total and active calories kept separate.

The coaching guardrails follow the [American Heart Association's general HR
guidance](https://www.heart.org/en/healthy-living/exercise-and-physical-activity/fitness-basics/target-heart-rates):
age-based maximum/zone estimates are population guides, not a measured personal
maximum or safety limit; medication and individual differences affect HR.
The [CDC talk test](https://www.cdc.gov/physical-activity-basics/measuring/index.html)
and perceived effort can add context. These are model instructions, not a
clinical assessment or deterministic fitness score. Tests verify current
owner-only data reaches the model; they do not claim to validate every generated
coaching statement.
