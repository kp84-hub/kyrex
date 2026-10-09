# L6 trainer-change monitoring

In Kyrex Chat Settings, **L6 trainer alerts** monitors the pinned Glofox branch's
8:30 **Group Fitness Class**. It uses no Facebook, workout OCR or LLM decisions.

Default preferences are off, Chat-only, hourly checks, and the next 14 calendar
days in America/New_York. Sundays are read from Glofox. The first valid
observation of each occurrence seeds its trainer silently. A changed trainer
must agree with a second read at least 90 seconds later before it is confirmed.
Missing, ambiguous or failed reads never erase the confirmed trainer. Failures
back off, and the settings show the last check and a bounded error reason.

Only the nearest upcoming occurrence of each weekday can alert. Confirmed
changes for later dates remain pending; if several accumulate before the date
becomes eligible, only the latest current trainer is announced. All versions
remain in history. Past classes never alert. An increasing version per class
date distinguishes A → B → A → B as three changes. There is no 24-hour cooldown.

Group messages contain exactly one line, for example:

> Your Wednesday class has a new trainer: Austin Ordonez.

## Deploy and enable

1. Deploy the server/worker and Chat changes from the same commit.
2. On Railway's worker, set `KYREX_LEVEL6_TRAINER_MONITOR_ENABLED=1`. Set it on
   the web service as well so the settings can report that it is configured.
3. Choose your running Calendar Bot in **L6 trainer alerts**, retain hourly
   checks and 14 days, and enable the monitor. Chat-only delivery needs no
   Browser Host. Its results appear in the saved **L6 trainer alerts** chat.
4. For group delivery, also set `KYREX_LEVEL6_SEND_ENABLED=1` on the worker.
   The chosen Calendar Bot must retain its normal Calendar preset, including
   `glofox:read` and `messages:send_level6`, and have an explicit Browser Host
   binding. Deploy/rebuild the Browser Host image from this commit; an older
   host rejects the new one-line alert payload.
5. On that host, verify `KYREX_GOOGLE_MESSAGES_CONVERSATION_URL` is the existing
   L6 Besties `/web/conversations/<id>` URL and that the persistent
   `google-messages` profile is paired to Google Messages. This reuses the
   existing pinned destination; the API cannot accept another recipient or
   conversation URL. A running/online host does not prove the profile is paired.
6. Select **L6 group chat** and save. No test message is sent by these controls.

The Sunday weekly preview flow is unchanged and still needs its normal review
and Send confirmation. The trainer monitor has its own explicit opt-in.

## Recovery and controls

Pause/resume and delivery mode are owner-scoped. Changes to settings invalidate
an in-flight read before it commits. A send already dispatched may finish after
pause. **Reset baseline silently** discards pending notifications and re-seeds
on the next valid read; it keeps version counters so delivery keys are not reused.

An intent is recorded before dispatch. A host error after a click, a timeout,
or a worker crash can leave delivery **unknown**. Interrupted sending rows are
recovered after the old worker's bounded lease expires (up to 35 minutes);
another worker cannot steal an active send. Unknown sends are never retried
automatically. Check the group first, then use **Resend (may duplicate)** in
alert history. Each explicit resend has a new attempt identity and is checked
against a fresh schedule before delivery; repeating the same HTTP request
cannot create another attempt. Stale/expired changes cannot be resent.

The host's receipt key includes owner, occurrence, version, manual attempt and
pinned destination, so identical wording in a later week is not suppressed.
Receipts and the cloud ledger reduce duplicates but cannot guarantee exactly-once
Google Messages delivery. A manual resend may repeat an already accepted send.

State is stored under `KYREX_DATA_DIR` in `level6_trainers.sqlite3` with restricted
file permissions. It must share the persistent volume across the worker and web
process, just like the task store. Guest tokens remain memory-only. Reads are
bounded to four pages of events, two pages of trainers, and 10 seconds per HTTP
request. Watch provider rate limits during the first week.
