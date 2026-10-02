# Android Messages (SMS, read-only)

Messages appears in the Connections hub alongside Calendar and Gmail. This
first version reads the latest **100 received SMS texts** from an Android phone
through a manually run Termux bridge. It does not send messages, read RCS,
read MMS attachments, or continuously monitor the phone. Connecting a browser
alone cannot grant Android SMS access.

## Phone setup

1. In Kyrex, open **Connectors → Messages → Connect**. This issues a one-time
   pairing code valid for 15 minutes. Keep the setup panel open.
2. Install [Termux](https://f-droid.org/en/packages/com.termux/) and
   [Termux:API](https://f-droid.org/en/packages/com.termux.api/) from F-Droid.
   Use the same installation source for both apps. Grant Termux:API the SMS
   permission in Android app settings. If Android blocks permission, no SMS
   access is available; do not assume the connection succeeded.
3. In Termux, run `pkg install python termux-api`.
4. Download the bridge from the Messages setup panel. To copy it from your
   Downloads folder into Termux, run `termux-setup-storage`, grant the requested
   storage access, then `cp ~/storage/downloads/messages_phone.py ~/`.
5. Run `python messages_phone.py pair`. Enter the exact HTTPS Cloud origin
   shown in the setup panel, then paste the pairing code at the hidden prompt.
   The code is not placed in shell history or URL parameters.
6. Run `python messages_phone.py sync`. The first successful upload moves the
   connector to **Connected** after **Check connection** is tapped.
7. In Chat, try `Show my texts`, `Find my text messages about school`,
   `messages: latest`, or `messages: search field trip`.

Run `python messages_phone.py sync` again whenever you need a fresh snapshot.
Chat returns up to 10 matching texts and includes the snapshot's UTC sync time.
The SMS Received timestamps are in the phone's local time. Search only covers
that snapshot, not the phone's entire history. SMS permission does not grant
access to Google Messages' separate RCS history.

## Disconnect and data

**Disconnect** revokes the phone upload credential, cancels pending pairing,
and deletes the stored snapshot. A new pairing also replaces the old phone and
clears its snapshot. On the phone, `python messages_phone.py forget` removes
the local credential; also disconnect in Kyrex to revoke it server-side.
Text returned in Chat remains in its conversation until that conversation is
deleted. Disconnect does not erase Chat history.

The Cloud encrypts snapshots using the existing connector encryption key
(`WEB_SESSION_SECRET`, or `KYREX_PROVIDER_SECRETS_KEY`). Without a usable key,
pairing and reads fail closed. Storage is SQLite under `KYREX_DATA_DIR`; deploy
with the same persistent data root used by other Cloud state. Backups of that
root may retain previous encrypted snapshots under your backup policy.

Phone credentials are stored mode 0600 and grant **upload only**: they cannot
search texts, access another account, or manage connections. Owner-authenticated
Cloud sessions are required to create a pairing, read/search, or disconnect.
Pairing codes and phone credentials are stored as hashes in the Cloud.
Uploads contain at most 100 messages and are bounded by count, field length,
and total request size. SMS contents are returned directly, never executed as
instructions or sent through an LLM for these read requests.

The bridge uses only Python's standard library and the read-only
[`termux-sms-list`](https://github.com/termux/termux-api-package/blob/master/scripts/termux-sms-list.in)
command. Termux's [SMS reader implementation](https://github.com/termux/termux-api/blob/master/app/src/main/java/com/termux/api/apis/SmsInboxAPI.java)
defines its sender, number, received and body fields. No SMS send command exists
in this bridge. Real phone permission and sync still need to be verified on the
owner's Android device after deployment.
