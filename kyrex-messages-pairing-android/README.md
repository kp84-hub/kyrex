# Google Messages protocol pairing test

Isolated Android test, application ID `com.kyrex.messages.pairingtest`. It does
not replace the earlier SMS-provider probe. The companion can now link to Kyrex Cloud. This isolated
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

Local history checks return only IDs/counts/match results. After separate Link and sync consent, bounded message text and metadata upload to the server address shown in Chat. There is no mark-read, media upload, analytics or background service. Neither bodies nor credentials are logged. Library logging is disabled. This is still a paired device interacting
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
trying again. A successful response requires the exact completed outgoing message in Google
Messages history; it does not prove that the recipient received it. Verify both the phone's conversation and the
recipient's delivery before declaring the send test passed. Send previews stay local and are never logged. After account linking, sent messages may be included in the text snapshot.

Phone observations so far: emoji pairing, exact history matching, incoming events,
and reopen/reconnect worked for the tester. Password followed by Google's two-factor
approval can hang in embedded sign-in; that route remains unresolved. The incoming
event counter alone does not prove continuous background reception.

## v0.3 Kyrex account link and snapshot sync

The frontend and backend must be deployed together. In Chat > Connections >
Messages > Connect, copy the server origin and 15-minute one-time code. In this
companion, connect Google Messages, enter those values under Link Kyrex account,
and confirm Link and sync after reading the upload disclosure. The phone stores
the upload-only credential encrypted under a separate Android Keystore key.
Google cookies/session keys never go to Kyrex Cloud. Network calls require HTTPS
and reject redirects. Pairing codes and upload tokens are not logged.

Sync replaces an encrypted owner-scoped snapshot with up to 100 text messages:
10 messages from each of 10 recent inbox conversations, sorted by message time.
Incoming and outgoing messages are included. Attachments, empty bodies and text
longer than 10,000 characters are skipped. Protocol labels describe the current
conversation, not the transport of each historical message. A failed RPC aborts
the snapshot upload; the previous complete snapshot remains readable.

After first sync, Chat moves Messages to Connected and displays last synced.
Ask **Show my texts** or **messages: search school**. Reads use that owner's
snapshot directly, without interpreting message content as agent instructions.
Chat is read-only; sending still requires the phone's explicit review/Send dialog.

Sync now is manual. New protocol events trigger throttled sync while the activity
is visible. Android background/force-stop operation is not guaranteed. Reopening
restores the account credential and Google pairing; check the cloud sync time.
Disconnect in Chat deletes the snapshot and revokes tokens and pending codes.
Remove account link on the phone stops uploads locally but does not delete cloud
data. Forget Google pairing only clears Google credentials.

Acceptance test: pair account, verify a known SMS and RCS body via Chat, receive
a new message with the companion open and check Chat after sync, reopen and
sync without relinking, then disconnect in Chat and confirm uploads are rejected.
Do not merge until this phone/cloud end-to-end flow passes. Password/2FA embedded
Google login remains a separate unresolved issue; successful emoji login is the
previously tested route.

## v0.4 Send from Chat and read one reply

Update the companion and separately enable **Allow sends confirmed in Kyrex Chat**.
The setting is saved with the encrypted account link and is off by default.
In Chat, use `Text Ethan: Are you home?` or `Send a message to Ethan saying Hello`.
Recipient resolution uses conversation metadata in the bounded snapshot, never
message body text. Ambiguous names require a more specific name. Only existing
conversations are supported. A scoped command asks the phone to verify the
conversation, SIM and all recipients. Chat displays the exact preview and requires
**Send** or **Cancel**. Drafts expire; each claimed send is single-use. The phone
rechecks recipient membership/SIM before sending. No send is automatically retried,
including after a lost acknowledgement or restart. Accepted means submitted to
Google Messages, not delivered. Verify recipient delivery during testing.

Commands and preview text are encrypted in the owner's message database. The
phone credential can only handle its owner's commands and upload snapshots;
it cannot read Chat/cloud snapshots. Disconnect or a new pairing revokes pending
commands. Turning off Chat sending cancels unclaimed commands on the next poll;
an already claimed send cannot be cancelled.

Keep the companion running. Polling continues for up to five minutes after
switching apps so the same phone can confirm in Chat. Android may kill the app
sooner; this does not provide an unattended background service. If Chat says
Waiting for phone, return to the companion. Pending preparations expire after
five minutes; verified previews expire after 90 seconds.

Click **Read reply** on the send card to display just the newest incoming message
after that send's confirmation, from the latest snapshot. Or ask `What did Ethan
reply?`, `Read the latest message from Ethan`, `Did he reply?`, or `Read reply`.
Follow-ups use the last message conversation in that Chat. Snapshot time is always
shown; no match means only that the current snapshot lacks a newer reply. Only
10 messages per recent conversation are synced, not complete history.

Phone acceptance: verify one SMS and one RCS submission, exact recipient preview
(including a group), Cancel, no duplicate on repeated confirmation, Read reply
without unrelated threads, reopen/no expired send execution, and revoke in Chat.

### v0.5 send verification

Approval tokens remain private single-use confirmations. The Google protocol transaction ID is separate and uses the upstream `tmp_` format. After one send RPC, the companion reads the intended thread for up to 20 seconds and matches transaction ID, conversation, sender, and exact text. Only completed, delivered, or displayed statuses succeed. Pending, missing, or failed outgoing messages produce an unverified outcome; no send is retried. History verification does not replace recipient delivery confirmation.
