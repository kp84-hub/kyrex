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
   another login. For background operation, explicitly enable Keep Messages connected in v0.8 or later.
8. For live capture, keep the test open and have a trusted contact send a message.
   The new-message event counter is diagnostic, not a delivery or completeness
   guarantee: the protocol may emit updates as well as newly created messages.
9. **Forget pairing on this phone** removes local session material. Remove this
   device in Google Messages > Device pairing to revoke the remote pairing too.

## Privacy and limits

The companion declares INTERNET and ACCESS_NETWORK_STATE. v0.8 adds FOREGROUND_SERVICE, FOREGROUND_SERVICE_REMOTE_MESSAGING and the Android 13+ notification permission for its opt-in Messages service. It does not request SMS-provider, contacts, accessibility, or battery-exemption permissions. Google session cookies and paired-device
keys are encrypted using Android Keystore AES-GCM. Android app backups are disabled.
The test uses a separate WebView data directory and clears its cookies before fresh
sign-in and after pairing. Login screenshots are disabled. Session capture only
occurs at the exact HTTPS `messages.google.com/web/config` endpoint.

Local history checks return only IDs/counts/match results. After separate Link and sync consent, bounded message text and metadata upload to the server address shown in Chat. There is no mark-read, media upload or analytics. Background operation is separately enabled in v0.8. Neither bodies nor credentials are logged. Library logging is disabled. This is still a paired device interacting
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

## v0.6 connection recovery

Returning to the companion checks the Messages connection with a bounded read.
Temporary listener/phone failures schedule that same check. If the connection is
healthy, the current bridge and reviewed drafts stay intact. Otherwise the
companion restores its encrypted saved pairing with retries that back off from
five seconds to one minute. A successful conversation read resumes snapshot sync
and Chat command polling. Google session rejection/unpairing, unreadable local
pairing, Forget, and activity shutdown stop recovery until explicit reconnection.

Recovery runs only while the activity is visible or within its existing
five-minute command window. It does not add an unattended background service.
Android may suspend or kill the app; reopening still uses the saved pairing.
Preparations/sends in progress block connection replacement. A replaced bridge
loses its old confirmation tokens, so an old preview may need to be prepared
again. Recovery never repeats a send RPC or copies a send token to a new bridge.

Phone verification: leave the companion for Chat and return; test a temporary
network interruption and recovery without tapping Reconnect; confirm snapshot
sync resumes; confirm a reviewed draft survives a healthy resume; and check that
an expired/revoked pairing asks for login. Check any uncertain send in Google
Messages before preparing another. Installing the v0.6 APK is required; a cloud
redeployment alone does not update the phone app.


## v0.7 live status in Settings

Open Kyrex Settings > Connections > Messages to see a live phone check-in and
Google Messages readiness separately from the saved text snapshot. The open
panel refreshes every five seconds; closing it stops status requests. Nothing
is added to the main Chat screen.

The v0.7 phone sends a status-only heartbeat every ten seconds during its
existing foreground/five-minute command window. It cannot claim a command,
enable sending, or repeat a send. Readiness requires the verified Google
connection; reconnecting and a stopped/rejected pairing have distinct states.
After 45 seconds without a heartbeat, the status becomes unreachable even if
a readable snapshot exists. Older companions show live status unavailable.

Phone validation: install v0.7, restore the connection, then switch to Kyrex
Chat and open Messages settings. Verify connected, reconnecting while the
Google connection recovers, and unreachable when Android stops the companion
or the five-minute window ends. Saved texts should still be readable. Do not
resend an existing workout to test presence. This adds visibility, not an
unlimited Android background service.


## v0.8 background Messages connection

The service now owns the Google pairing, connection recovery, snapshot worker,
command polling and independent status-only heartbeat. MainActivity only binds to
that same runtime for settings, Google sign-in, local history checks and explicit
send review. Leaving or destroying the Activity does not close the opted-in
service or retire an otherwise healthy connection's draft tokens.

Install the v0.8 APK and confirm **v0.8** in its heading or Android App info.
Existing encrypted Google pairing, account link and sending preference retain
their storage names. If Android refuses Update because these test builds have
different debug signing certificates, uninstall/reinstall and pair again.

After Google Messages and Kyrex account linking, tap **Keep Messages connected**
and confirm Enable. Background connection is off by default and independent of
Chat sending permission. The remoteMessaging foreground service immediately
shows an Android connection notification with an Open action and **Stop**.
Notification permission denial does not prevent the foreground service; Android
still exposes it through active-app controls. No message bodies or credentials
appear in the connection notification or logs.

The previous five-minute screen-owned command window no longer controls the
opted-in service. Background connection continues until stopped, unlinked,
unpaired/rejected, or stopped by Android. Network changes trigger a bounded
connection probe and the existing backoff recovery; healthy probes preserve
reviewed drafts. Cloud heartbeat failures are shown in the companion; successful
cloud acknowledgements must still be fresh before its notification says connected.
Revoked cloud credentials stop further sync/poll/heartbeat attempts and request a
new pairing code. Cloud Settings still expires the last check-in after 45 seconds;
no green status is fabricated to hide a suspended or unreachable phone.

A sticky process restart restores the opted-in runtime from encrypted storage.
No outgoing text, command, confirmation token or acknowledgement is persisted or
replayed by the service. Remote command claims and single-use native drafts keep
their existing expiry/owner/send-confirmation safeguards. Turning on background
connection does not enable Chat sending. **Stop** disables restart opt-in; Remove
account link and Forget pairing also stop the background service.

This is not a guarantee against force-stop, reboot, Google revocation or Android
Doze/vendor power saving. There is no boot receiver, wake lock, exact alarm or
battery-exemption bypass. An optional Android battery-settings shortcut lets the
user choose unrestricted battery use. Reopening the companion restores a saved
opt-in from a visible Activity; the service never starts itself from an arbitrary
background event. Android requires its own connection notification; Kyrex Chat's
status remains exclusively in Settings > Connections > Messages.

Validation: Go protocol tests plus Java unit tests, Robolectric service/runtime
regressions on APIs 28/35, Android lint and APK assembly. Tests cover one runtime
across UI detach/reattach, no five-minute timer, explicit Stop/restart opt-in,
missing/rejected pairing, unchanged sending consent, heartbeat failure visibility,
revocation stopping retries and expired cloud acknowledgements.

Phone acceptance (read-only; do not resend the workout):
1. Enable Keep Messages connected, return to Kyrex Chat, and leave the companion
   screen closed for more than six minutes. Settings should show fresh check-ins.
2. Ask Show my recent texts with Ethan The Neighbor. Verify snapshot time.
3. Briefly interrupt network access, restore it and verify automatic check-ins and
   sync recover without tapping Reconnect. Check Settings when the screen has
   been locked; Android power saving can still interrupt its network.
4. Stop from the notification. Check-ins must expire; saved texts remain readable.
   Reopening should show background connection off until explicitly enabled again.
5. Separately verify a preview-only message and Cancel. A real send is optional
   and requires a new explicit confirmation; no prior workout/send is reused.
