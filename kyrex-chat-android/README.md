# Kyrex for Android — unified native test build

Version 0.2 bundles the existing Chat UI, Samsung Health, background Voice and
phone-local Google Messages into one Android package (`com.kyrex.chat`) and one
launcher icon. Connections is the common setup/status entry point. Each native
feature has separate consent and can be enabled or disconnected independently.
Ordinary browser Chat remains available on phones and PCs without installation.

This completes the source consolidation, not real-phone acceptance or public
release. The new app replaces the need for three separate feature installs once
its phone acceptance checks pass. It still requires one Android installation
for native device APIs. It does not turn the existing PWA into a Health Connect
or background microphone client.

## Build and deployment

Use JDK 17, SDK 36, build tools 35.0.0, NDK 27.2.12479018 and Gradle 8.11.1.
Build the existing Go Messages bridge as documented in
`kyrex-messages-pairing-android/README.md` and run
`python kyrex-voice-android/prepare_assets.py` for checksum-verified wake assets.
The integrated Actions workflow performs these steps and tests both adapters.

```sh
gradle --no-daemon -PkyrexChatOrigin=https://chat.example \
  :app:testDebugUnitTest :app:lintDebug :app:assembleDebug
```

Set Cloud's `KYREX_ANDROID_CHAT_ORIGIN` to that exact trusted HTTPS origin.
Configure `KYREX_ANDROID_CERT_SHA256` with this APK's public signing fingerprint
(colon separated) for verified TWA display. A mismatched/unconfigured certificate
keeps the browser security toolbar. The fingerprint comes from the built APK;
never disable domain verification. A different signed build needs its own
fingerprint. Existing API, device and Windows pairing paths stay compatible.

Use one retained signing keystore outside Git for maintainable updates. Locally
set `KYREX_CHAT_ANDROID_KEYSTORE`; CI accepts a standard Android debug-format
keystore through the `KYREX_CHAT_ANDROID_KEYSTORE_B64` secret. Unconfigured CI
uses temporary debug keys, suitable for initial tests; differently signed builds
require reinstall/reconnect. Do not distribute a signing key in build artifacts.

## Setup in the unified app

Open Kyrex and sign in using the normal browser account. Go to Connections:

- Samsung Health → Health settings on this phone. Connect your Kyrex account,
  grant selected Health Connect reads, then sync the last seven days. Optional
  automatic sync needs its separate background grant. Samsung-only filtering,
  bounded uploads, owner isolation and normal retention remain unchanged.
- Voice → Voice settings on this phone. Connect the account, then explicitly
  choose Start listening or Talk now and grant microphone access. The existing
  listening lease prevents two devices from submitting the same wake. Stop is
  available in the persistent notification. Merely signing in never starts it.
- Messages → Messages settings on this phone. Pair Google Messages using the
  existing phone flow and emoji confirmation, then connect your Kyrex account
  through the browser confirmation. Google keys stay on the phone. Background
  connection and Allow sends confirmed in Kyrex Chat are separate opt-ins.
  Sending still uses the exact recipient/message preview, explicit confirmation,
  single claimed attempt, receipt and no automatic send retry.

These are native settings within the same package, not separate feature apps.
The native shell opens existing browser Chat and does not create another AI
engine, task queue, approval system or general JavaScript-to-Android bridge.
Notification taps return to the relevant unified settings screen.

## Migration and phone acceptance

1. Finish/check any confirmed send in progress. Stop listening and background
   Messages connection in the old companions. Keep them installed for rollback
   until the new flows pass; do not run two phone protocol sessions together.
2. Install the unified app and connect only the features you want. Android
   package sandboxes and Keystore keys cannot be transferred silently; Health
   needs new OS grants and Messages needs a deliberate Google re-pair. The
   same Kyrex sign-in is reused; no server-address or pairing-code copy is needed.
3. Sync Health and verify the readings/timestamps in Chat. Test permission
   denial and turning automatic sync off without affecting other modules.
4. Start native Voice, test a read request, lock/unlock the phone, and verify
   Stop, listening takeover and reply voice settings. Native requests use the
   same Chat conversation and execution/approval path as before.
5. Verify saved Messages, new-message sync and live status after closing setup.
   Prepare a draft, check every recipient and text, then cancel. Only conduct a
   real send when the owner explicitly confirms it in Chat. Confirm no replay
   after reconnect, screen changes or a process restart.
6. Sign out/change account. Subsequent native Health uploads, Voice operations
   and Messages upload/poll/ack/heartbeat must be rejected. Voice heartbeats stop
   listening; revoked Messages links stop background activity. An accepted
   external task keeps its existing reconciliation semantics.
7. Retire the old feature apps only after these checks pass. The PWA shortcut
   can also be removed if the user chooses the native launcher as their entry.

Messages migration preserves saved cloud texts and terminal/unknown receipts,
rotates the old phone credential and disables sending by default. A queued or
claimed confirmed send blocks migration until its outcome is checked; pending
unconfirmed drafts are cancelled, never moved/replayed. Existing generic legacy
pairing retains its previous replacement behavior. Health readings and the
shared voice conversation/speech settings remain in the original owner stores.

## Authentication, isolation and licenses

Native setup uses per-feature private, encrypted pending verifiers and state.
Browser consent binds the displayed owner, feature, deployment and live session
into a one-use, two-minute S256 handoff. Only narrow existing device credentials
are redeemed; browser cookies and provider credentials never enter callbacks or
native setup pages. Logout/expiry/account replacement blocks further native
requests. Existing connector revocation and per-operation policies still apply.

Health, Messages Google sessions, Messages cloud link and Voice use separate
credential preferences and Keystore aliases. Forgetting Health must not erase a
Messages pairing. Services are independently permissioned and not exported.
Microphone/background operation requires explicit native user action.

The Messages module and bridge carry AGPL-3.0-or-later notices; the APK includes
its license, the original MIT license, third-party notices and an accessible
corresponding-source link in Connections → Android app licenses and source.
Original module license notices are retained. Generated model/JNI files are
prepared during the build and are not checked into the repository.

Controlled HTTP/SQLite/Robolectric/Go tests establish code behavior and build
compatibility. They do not establish real Google pairing, microphone/wake
accuracy, Health Connect availability or successful phone migration; complete
the acceptance checks on the actual phone before retiring working companions.
