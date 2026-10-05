# Google Messages protocol pairing test

Isolated Android test, application ID `com.kyrex.messages.pairingtest`. It does
not replace the earlier SMS-provider probe or change Kyrex Cloud. This standalone
subproject is AGPL-3.0-or-later; the rest of the repository retains its existing
license.

The login approach was identified by reviewing MirrorMsg at commit
`6c748af0b69867928638f246f1cd715cb1d77cfb`: an unmodified Android WebView,
CookieManager session capture, and libgm Google-account emoji pairing. The
adapter here is deliberately minimal, with an explicitly confirmed text send test. It pins libgm v0.2605.0.
Google may reject embedded login; that is a test result, not something to bypass.

## Phone test

1. Install the APK and open **Kyrex Messages Pairing Test**.
2. Tap **Connect Messages**, read the disclosure, and sign in with the account
   selected in Google Messages > Device pairing.
3. If Google rejects login, tap **Google blocked sign-in** and report that result.
   No user-agent spoofing, browser-security disabling or manual cookie workaround
   is included.
4. If the test shows an emoji, switch to Google Messages and confirm that emoji,
   then return. Successful pairing alone is not reported as Connected: a phone
   conversation-list response must also succeed.
5. Paste distinctive exact text from a known RCS message, choose its conversation,
   and verify **Exact text FOUND**. Also try a known SMS conversation.
6. Tap **Load older messages** in a long thread. The count deduplicates message
   IDs. Paging is 50 messages at a time, with a visible next-page indication.
   Conversation discovery is bounded to 100 inbox threads, not the whole inbox.
7. Close/reopen the app. Its encrypted saved pairing should restore without
   another login. Do not expect unattended background operation from this spike.
8. For live capture, keep the test open and have a trusted contact send a message.
   The new-message event counter is diagnostic, not a delivery or completeness
   guarantee: the protocol may emit updates as well as newly created messages.
9. **Forget pairing on this phone** removes local session material. Remove this
   device in Google Messages > Device pairing to revoke the remote pairing too.

## Privacy and limits

The only Android permission is INTERNET. Google session cookies and paired-device
keys are encrypted using Android Keystore AES-GCM. Android app backups are disabled.
The test uses a separate WebView data directory and clears its cookies before fresh
sign-in and after pairing. Login screenshots are disabled. Session capture only
occurs at the exact HTTPS `messages.google.com/web/config` endpoint.

There is no Kyrex backend URL, upload, delete, mark-read, contacts fetch,
analytics or background service. Read text is compared on the Go side and only
message IDs/counts/match results cross into the UI. Neither bodies nor cookies are
logged. Library logging is disabled. This is still a paired device interacting
with Google Messages; session/activity effects depend on Google's protocol.

Google authentication and live-device behavior have not been verified until a
tester completes the steps above. A missing exact match is not evidence that
all RCS is unsupported or that the whole archive was read. A successful match is
evidence for that message only. SMS/RCS labels are protocol conversation labels.

Build: Go 1.26.5, pinned gomobile/gobind revision, Android SDK 36, NDK 27.2,
JDK 17, Gradle 8.11.1. GitHub Actions runs adapter unit tests, Android unit tests,
lint and an arm64 debug APK build. `go mod tidy` resolves pinned libgm's transitive
dependencies; the app includes upstream notices and licensing references.

## v0.2 send test

Select an existing conversation, type a message, then tap **Review message**.
The app fetches the conversation to verify the sending SIM and every participant,
and shows the exact message and all recipients, including group members. Only
**Send** in that dialog submits it. Cancel does not send. No new conversations,
attachments, or automatic sends are included. Google Messages chooses the transport;
the conversation label does not prove that a particular send used RCS.

Confirmations expire after two minutes and are single-use. Send failures never
retry automatically. An ambiguous result requires checking Google Messages before
trying again. A successful response means Google Messages accepted the submission,
not that the recipient received it. Verify both the phone's conversation and the
recipient's delivery before declaring the send test passed. Messages and recipients
stay local and are never logged or uploaded to Kyrex.

Phone observations so far: emoji pairing, exact history matching, incoming events,
and reopen/reconnect worked for the tester. Password followed by Google's two-factor
approval can hang in embedded sign-in; that route remains unresolved. The incoming
event counter alone does not prove continuous background reception.
