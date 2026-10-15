# Android capabilities inside Kyrex Chat

Status: feasibility audit and proposed implementation, 2026-10-15.
Runtime source baseline: `a359780fa582931a6edec89f6a88ee70a07afae2`.
This extends the [integration audit](integration-audit.md) and
[platform architecture](universal-agent-platform.md). No Android packages or
runtime endpoints change in this documentation update.

## Product decision

People should open Kyrex Chat, sign in and connect what they need. Ordinary
service use must not require finding a development APK, copying a server address
between apps, or installing Health, Messages and Voice separately.

Use two delivery modes for the same product:

- **Kyrex Chat on the web/PWA:** no download required for ordinary chat,
  account/API connectors, visible-tab voice and observing accepted cloud tasks.
  Installing the PWA is an optional convenience, not extra Android authority.
- **One optional Kyrex Chat Android app:** shared Chat experience plus native
  device capabilities for users who choose Health Connect, screen-off voice or
  phone-local messaging. It replaces the need for separate Kyrex feature apps.

The second mode is still an app installation. It is required only for features
that actually need device APIs. Do not promise that all current companion
features can run in a browser or be performed by a remote VPS. Existing source
apps remain supported until replacement paths pass migration checks.

## All current Android application projects

Build identities and versions below are source values, not installed-device or
latest-release assertions. No additional Android application project was found
in the reviewed repository inventory. The Windows voice client is a separate
desktop companion, not a fourth Android application.

| Current app | Build identity | Native responsibilities | Proposed destination |
| --- | --- | --- | --- |
| [Kyrex Health](../../kyrex-health-android/app/build.gradle.kts), version 0.5 | `com.kyrex.health` | Health Connect permission UI and reads; Samsung-origin filtering; encrypted upload credential; manual sync and optional WorkManager sync | Native health module inside one Kyrex Chat app; connection, health status and reports stay in shared Chat |
| [Kyrex Messages](../../kyrex-messages-pairing-android/app/build.gradle.kts), version 0.9 | `com.kyrex.messages.pairingtest` | Go/libgm paired Messages client, local session keys, bounded text sync, foreground messaging service and reviewed send execution | Prefer hosted/browser connection for users who choose it; retain optional local messaging module in the one Chat app where local session custody is desired |
| [Kyrex Voice](../../kyrex-voice-android/app/build.gradle.kts), version 0.8 | `com.kyrex.voice` | Local wake detection, Android recognition, microphone foreground service, lease heartbeat, temporary reply playback and encrypted device credential | Native voice module in the same Chat app, using shared speech settings and the existing task pipeline |

These apps are already connector/device adapters rather than independent AI
engines. Preserve that separation when consolidating them; do not embed a
different reasoning loop or new approval system in Android.

## What can run without a separate app?

| Feature | Web/PWA feasibility | Required implementation or limit |
| --- | --- | --- |
| Chat, Bots, email, calendar, Oura and GitHub | Existing cloud/API model needs no Kyrex Android companion | Consistent account setup/status and shared task/review contracts |
| Talking while Chat is visible | Already implemented with browser microphone, transcription and replies | Current browser voice stops on hidden/pagehide; retain permission and listening-state controls |
| Wake listening with screen off or Chat closed | Do not offer a browser/PWA guarantee | Native user-started microphone foreground service, explicit Stop and existing voice lease controls |
| Samsung Health data via Health Connect | Not available to ordinary website JavaScript through the implemented SDK path | Native Android module and OS data permissions; VPS cannot read the phone's local Health Connect store |
| Reading Google Messages | Existing enrolled browser-host connector offers a potential no-Kyrex-APK path | Repair explicit new browser connection flow and integrate setup into Chat; requires supported Google pairing and a healthy account-isolated host |
| Sending Google Messages | Current generic Chat sends execute on a consenting paired phone | A generic hosted/browser send adapter needs its own exact-recipient review, single claimed attempt and outcome verification; current browser read access is not send consent |
| Keeping an accepted cloud task running after screen sleep | Must be a server task capability, not a phone app requirement | Shared durable task IDs/cursors, worker recovery and effect receipts; no need for a phone microphone service to keep a booking task alive |

Health conclusion follows the documented Android SDK/manifest/permission
contract and the actual HealthSync implementation. Android documents a
microphone foreground service for continuing capture in the background, with
while-in-use/start restrictions. These are platform constraints, not a reason
to require native installation for unrelated web services.

## Findings in current code

### Health

[HealthSync](../../kyrex-health-android/app/src/main/java/com/kyrex/health/HealthSync.kt)
uses `HealthConnectClient`, per-data-type grants, Android Keystore and bounded
HTTPS uploads. It filters records to Samsung Health origin. The
[manifest](../../kyrex-health-android/app/src/main/AndroidManifest.xml) declares
health permissions, including optional background reads; the
[worker](../../kyrex-health-android/app/src/main/java/com/kyrex/health/HealthSyncWorker.kt)
checks pairing, OS permission, feature availability and opt-in before syncing.

[ConnectionsSettings](../../kyrex-chat/src/components/ConnectionsSettings.jsx)
currently creates a pairing code and tells users to open the Health companion.
Move the controls into the unified app with a native permission handoff. On
other devices, Chat shows synced data and the originating phone's last successful
sync. Do not display a web-only permission switch that cannot grant Android
access. Missing background support retains manual sync. If Health Connect
supports more origins later, source selection and new consent must be explicit;
do not silently broaden the current Samsung-only upload.

### Messages

The [Android manifest](../../kyrex-messages-pairing-android/app/src/main/AndroidManifest.xml)
uses a `remoteMessaging` foreground service, not direct SMS-history permissions.
The [Go bridge](../../kyrex-messages-pairing-android/bridge/bridge.go) implements
paired Google Messages history and confirmed sends; its
[session store](../../kyrex-messages-pairing-android/app/src/main/java/com/kyrex/pairing/SessionStore.java)
keeps Google session material encrypted on the phone. Cloud receives selected
text snapshots and device status, not those Google session keys.

There is already a different architecture in
[WebMessages](../../kyrex-cloud/web_messages.py),
[host connector](../../browser-host/messages_connector.py) and the
[browser setup page](../../kyrex-cloud/web/backend/messages_setup.html): an
enrolled Browser Host retains Google cookies and Chat forwards transient setup
input. However, the current
[API](../../kyrex-cloud/web/backend/messages_api.py) starts phone pairing at
`/messages/connect`; `/messages/browser` only allows screen/input/finish actions.
`WebMessages.rpc` requires a connect step to establish a new pairing record.
The [connection projection](../../kyrex-cloud/device_messages.py) explicitly
retains existing browser pairings while new connections use the phone.

Therefore this is reusable legacy infrastructure, not a working new-user
download-free setup flow. Add an explicit connection-mode/start contract,
account/host selection, inline setup/status and cancellation. Test first-time
pairing, account challenges, disconnect and reconnect on real devices before
advertising it. Google phone-side pairing approval can still be necessary; no
Kyrex companion does not mean no interaction with Google Messages.

Do not iframe Google's login or ask the LLM to handle credentials. Reuse a
controlled human sign-in handoff. Moving session custody from the phone to a
host is a disclosed connection choice, not an invisible migration. Phone
snapshots and browser reads must retain their different freshness/coverage
labels. They must not silently substitute for one another.

Keep phone-local messaging available as an optional module in the unified app
for users choosing that custody model. A hosted protocol runner is another
possible adapter, but running current mobile Go code on a VPS is not an
implemented service. Preserve existing component licenses and notices when
moving modules; the Messages sources carry AGPL identifiers and their own
[license](../../kyrex-messages-pairing-android/LICENSE).

### Voice and the existing PWA

[VoiceControls](../../kyrex-chat/src/components/VoiceControls.jsx) stops browser
voice on visibilitychange/pagehide. The
[PWA manifest](../../kyrex-chat/public/manifest.webmanifest) provides standalone
presentation; the [service worker](../../kyrex-chat/public/sw.js) caches a public
offline page/icon and does not run private chat jobs or microphone capture.
Adding another service-worker handler or removing the visibility listener does
not establish reliable always-on Android wake listening.

The native [VoiceService](../../kyrex-voice-android/app/src/main/java/com/kyrex/voice/VoiceService.kt)
and [WakeListener](../../kyrex-voice-android/app/src/main/java/com/kyrex/voice/WakeListener.kt)
already isolate the device work. Reuse them with the same shared speech settings,
listening lease, Stop notification and no-command-replay rules. Starting native
voice from the unified app must not also start browser capture. Listening stays
explicit; do not auto-start it when an account signs in or a device boots.

## One app, shared UI and explicit native modules

Proposed structure: keep React Chat and the cloud runtime shared; extract native
health, voice and optional messaging into modules under one Android application
identity and release. Chat Connections is the common entry point for permission
setup, device selection, status and disconnect, on web and Android.

Use a small native host with shared web UI and bounded native handoffs. A
Trusted Web Activity with native capability screens is a candidate starting
point because it keeps Chat in the user's browser. Chrome documents that its
host cannot directly access browser cookies/storage; it is not an automatic
native API bridge. Prove authenticated handoff and return-to-Chat behavior in a
vertical prototype before selecting the final host implementation. A plain
Bubblewrap/TWA wrapper by itself does not consolidate the native services.

If a WebView-based host is selected instead, use narrowly defined, versioned
native messages restricted to the verified Chat origin, never an unrestricted
JavaScript-to-Android bridge. External pages/login must not inherit native
capabilities. This hosting choice does not change the backend contracts.

One sign-in should establish the app's account identity. Each device module
still obtains a narrow revocable grant after its own consent. Browser cookies
must not be copied into native storage. Use a server-mediated one-time,
short-lived handoff bound to the initiating device, account, origin and proof
of possession; backend derives owner rather than trusting the UI. Never put
long-lived tokens or provider credentials in deep-link URLs.

Changing account or signing out stops feature services, cancels scheduled sync
and revokes the appropriate device grants before another account can use them.
Native tokens remain in Keystore-protected storage. Permission denial for Health
cannot disable ordinary Chat; microphone permission cannot grant message access.
Health, messaging and voice stay independently enabled and revocable.

Self-hosted Chat origins need an explicit trusted-origin configuration and
supported authentication handoff. Do not hardcode the operator's hostname or
allow arbitrary navigated web pages to call native modules.

## Migration and release gates

1. **Unify discovery/setup in Chat.** Advertise web, native and host-dependent
   capabilities honestly; separate selected connection mode and resource. Keep
   old pairing compatible while introducing the new account/device contract.
2. **Prove one no-companion Messages path.** Complete browser connect/start and
   inline pairing/status for a fresh account. Validate reading first; implement
   generic sending separately with the existing review and receipt semantics.
   Browser-host provisioning should become managed service setup, not a Docker
   command every end user must run.
3. **Build one native Chat vertical slice.** Sign in, request one Health grant,
   sync into the existing owner store and view results in shared Chat. Validate
   native/web auth handoff before moving the other modules.
4. **Move voice and optional local Messages.** Reuse native adapters and cloud
   endpoints behind one device identity with separate grants. Preserve listening
   leases, message participants/SIM checks, snapshot bounds and no effect replay.
5. **Ship one maintained Android product.** Stable app ID, retained signing key,
   authenticated updates and version compatibility replace development APK
   distribution. Existing three package identities cannot silently transfer
   sandboxed app data or Keystore keys into a new app; plan explicit re-sign-in,
   permission grants and reconnect without deleting existing cloud records.
6. **Retire separate apps after parity.** Disable duplicate scheduled sync and
   listeners, preserve pending-send outcomes and make old-device revocation
   visible. Do not delete working companions during the feasibility stage.

| Acceptance case | Required result |
| --- | --- |
| New user opens Chat in Android browser | Can sign in, use cloud connectors and talk while visible without a Kyrex APK |
| Fresh browser Messages connection | Connects from Chat without phone-companion installation; host/account pairing verified; sync/read provenance accurate |
| Web-only Health request | Honest native requirement; ordinary Chat and Oura continue working; no fake permission prompt |
| Single Android Chat install | Health permission and sync, background voice and optional local messaging available through one product with separate consent |
| Screen off after task accepted | Task remains server-owned and reattachable without another POST, regardless of voice microphone state |
| Revoke one feature or change account | No cross-account data, stale native bridge access, continuing sync or unintended send; unrelated features remain usable |
| First unified-app upgrade | Existing cloud data preserved; re-grants are deliberate; old workers/listeners cannot duplicate sync or external sends |
| Native host opens external content | Content cannot invoke native operations or receive auth/device tokens |

These are implementation gates, not passing tests. This follow-up reviewed
source and platform documentation; it did not build a unified Android app,
change the production setup UI or verify new live Google pairing.

## Platform references

Checked 2026-10-15; use official documentation when implementing version-specific
permissions and hosting behavior:

- [Android Health Connect setup and permissions](https://developer.android.com/health-and-fitness/health-connect/get-started)
- [Android foreground service types: microphone and remote messaging](https://developer.android.com/develop/background-work/services/fgs/service-types)
- [Chrome Trusted Web Activity overview and native/browser separation](https://developer.chrome.com/docs/android/trusted-web-activity/)
