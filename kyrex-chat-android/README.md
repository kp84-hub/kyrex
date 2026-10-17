# Kyrex Chat for Android — first integrated test build

Version 0.1 contains the existing browser-rendered Chat and the existing native
Samsung Health adapter in one package (`com.kyrex.chat`). Health settings open
from Chat Connections. The launcher uses Android Browser Helper's Trusted Web
Activity, with a normal Custom Tab fallback when domain verification is absent.
It does not embed GitHub login or copy browser cookies into Android. Ordinary
Chat stays available in the browser on phones and PCs.

Voice and Messages have **not** moved into this app yet. Keep their working
companions installed. This is a phone-validation build, not a completed migration.

## Build and deployment

Use JDK 17, SDK 36, build tools 35.0.0 and Gradle 8.11.1:

```sh
gradle --no-daemon -PkyrexChatOrigin=https://chat.example \
  :app:testDebugUnitTest :app:lintDebug :app:assembleDebug
```

The trusted HTTPS origin is required at build time; intent input cannot change
it. Deploy the corresponding Chat/Cloud changes and set
`KYREX_ANDROID_CHAT_ORIGIN` to that **same origin**. The feature is off until
configured; existing companion code pairing remains compatible.

For seamless TWA display, configure `KYREX_ANDROID_CERT_SHA256` on Cloud with
the APK signing certificate's public SHA-256 fingerprint (colon separated).
`/.well-known/assetlinks.json` supplies the matching statement. Multiple
fingerprints may be comma separated for an explicit signing-key transition.
Do not disable browser verification. Without a configured matching certificate,
Chat uses the browser's visible security toolbar; native Health handoff still
requires the account's explicit consent and device proof.

The Actions workflow builds the APK and publishes its public signing fingerprint.
Before maintaining in-place updates, retain one signing keystore **outside Git**.
Locally set `KYREX_CHAT_ANDROID_KEYSTORE` to its path; CI accepts the standard
Android debug-format keystore through `KYREX_CHAT_ANDROID_KEYSTORE_B64` secret.
Without that secret, Actions uses a temporary debug key: builds are suitable for
initial testing, but later builds may require reinstalling and reconnecting.
Never publish a signing keystore as a build artifact.

## Phone acceptance check

1. Install the new app alongside existing companions. Open Kyrex, sign in to
   Chat, then Connections → Samsung Health → Health settings on this phone.
2. Tap Connect health to Kyrex. The system browser shows the signed-in account
   and asks to connect this phone. Confirm, then tap Return to Kyrex.
3. Grant the Health Connect read permissions you choose and sync the last seven
   days. Return to Chat and verify the real readings and last successful sync.
4. Optionally enable automatic sync with its separate Android background grant.
   Test permission denial, app closure and reconnect. Manual sync remains usable
   without background access. Existing Samsung-only filtering remains in force.
5. Sign out of Chat. Further uploads from this integrated grant must fail;
   reconnect through the currently signed-in account before uploading again.
6. Verify ordinary Chat, Calendar and booking observation after returning from
   native settings. No new task submission or action replay occurs on return.

Do not uninstall the old Health app until real phone parity passes. Connecting
the new app rotates the owner's Health upload credential, stopping the old
phone/app from uploading. This preserves existing cloud readings. Package
sandboxes/Keystore keys cannot be silently transferred; the new app needs its
own OS permissions and the one-time account connection. Voice and Messages
remain independently connected and use their existing implementations.

## Security and limitations

Only Health ingestion authority reaches native code. The app creates an encrypted,
ten-minute pending verifier and private state. Browser consent mints a one-use,
two-minute code bound to its S256 challenge, owner, deployment and live browser
session. The return link contains this code and state, never the device token,
browser cookie or health data. Redemption checks the proof; the app checks its
stored state, exact callback and expiry. Redirects on the credential-bearing
HTTPS request are refused by the reused Health transport.

Device tokens remain Keystore protected. Logout, browser-session expiration,
account replacement or connector revocation blocks subsequent integrated
uploads; a background worker pauses on the rejected upload. Existing saved
readings retain their normal owner-scoped retention. Forget pairing removes
local credentials and stops scheduled work; the existing Chat Disconnect
control revokes the server connection and erases retained Health readings.

Health source/UI/worker and tests are compiled from `kyrex-health-android` rather
than forked. No native JavaScript bridge, independent AI engine, message sender,
booking executor, or new queue is added. Android OS permission behavior and
browser-to-app return still require the real phone acceptance above; controlled
tests and a successful APK build alone do not verify those device behaviors.
