# Kyrex Messages phone test

An Android capability probe, not a connected Kyrex messaging service. It tests
whether a Samsung phone exposes recent message history without replacing Google
Messages or requiring browser-host/VPS setup. Separate application ID and no
changes to Kyrex Health permissions or pairing.

Install the debug APK from the Build Messages Android test workflow. In the app:

1. Tap **Allow access** and approve Android's message-reading permission.
2. Tap **Check history**. The app reads at most 5,000 records from the last seven
   days through `Telephony.Sms`, displaying only counts and oldest/newest dates.
3. For an RCS check, confirm a recent message's transport in Google Messages,
   enter distinctive text from it, and check again. A match proves visibility
   of that specific record only. No match may mean provider exclusion, text
   mismatch, time-window exclusion or truncation. Never claim full RCS support
   based on a permission grant or counts alone.
4. Enter a trusted contact's number and tap **Open test draft**. Android opens
   the messaging app; the user checks recipient, sending SIM/number and transport
   and decides whether to send. Opening a draft is not delivery confirmation or
   proof of unattended SMS/RCS sending.

No INTERNET permission, notification listener, content logging, message storage,
automatic sending, mark-read operation or background service. Exact matching
occurs in memory; input state is not saved. Screenshots are disabled. The app
requests READ_SMS only after an explicit disclosure. Android/installer may
restrict this permission; denial or SecurityException is a test result, not a
reason to require shell commands or silently replace the default SMS app.

Build with JDK 17, Android SDK 36 and Gradle 8.11.1:
`gradle :app:testDebugUnitTest :app:lintDebug :app:assembleDebug`.

Before shipping a real connector, verify full history coverage, MMS/attachments,
RCS exposure on the target phone, background lifecycle and replies separately.
Add authenticated owner pairing, explicit upload consent, encryption/retention,
revocation and reporting of missing coverage. Google Play distribution requires
an eligible SMS permission use case (e.g. a real default Assistant handler or
approved exception); this debug probe does not establish Play Store eligibility.

Primary references:
- https://support.google.com/messages/answer/10252674
- https://developer.android.com/reference/android/provider/Telephony.Sms
- https://developer.android.com/guide/components/intents-common#Messaging
- https://support.google.com/googleplay/android-developer/answer/10208820
