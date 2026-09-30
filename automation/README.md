# VPS email automation — first milestone

The VPS polls Railway for new email IDs. Railway owns Gmail credentials, checks
the configured owner/sender/bot/conversation, and queues an exact Gmail read on
the existing worker. Chat recovers its result from the durable task record when
you return, even if the webpage was closed. The VPS never writes chat files or
shares Railway's SQLite database.

This first milestone delivers the existing bounded readable-email result, not
an LLM summary. Rules use an exact sender and a fixed existing bot conversation.
Jev importance classification, free-choice routing, unread badges, and phone
push notifications are subsequent milestones. There is no change to normal
interactive Chat routing or any automatic send/create/delete action. Do not
enable a rule until its sender and destination have been explicitly chosen.

## Configure Railway

Keep `KYREX_DATA_DIR` on the existing persistent volume. Set:

- `KYREX_AUTOMATION_ENABLED=1` (otherwise the VPS poll/event routes return 404;
  Chat Settings still lets you prepare a rule).
- `KYREX_AUTOMATION_OWNER`: the exact GitHub username owning the connection,
  bot, and destination conversation.
- `KYREX_AUTOMATION_TOKEN`: a dedicated random service credential, at least 32
  characters; generate with `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`.
  This credential can only use the configured read-only automation routes.

The sender, bot, and conversation are selected and saved in **Kyrex Chat →
Settings → Email automations**. Choose an existing conversation bound to the
bot, not a Chief of Staff chat which delegates to it. The bot must be running
and the owner must have connected Gmail read access. Kyrex rechecks both before
each poll/submission. It never selects another bot when the destination is
paused, missing, or invalid. The existing worker rechecks authorization again
when it executes the read.

## Start the VPS service

From an updated Kyrex repository on the VPS:

```sh
cp automation/automation.env.example automation/automation.env
chmod 600 automation/automation.env
```

Edit `automation/automation.env` with the Cloud HTTPS origin and the same
service credential. No Google token or bot LLM key belongs on the VPS.

```sh
docker compose -f automation/docker-compose.yml up -d --build
docker compose -f automation/docker-compose.yml logs --tail 30 email-watcher
```

This is a separate container from Browser Host, with no published ports. Keep
the named volume: it stores the first-run baseline and received task IDs. On
first successful complete scan, existing matching messages are recorded without
queueing them. Subsequent matching IDs are queued once per owner/rule/message.
A changed rule destination/sender establishes a new baseline. Server-side IDs
are independent of the destination version, so changing a rule cannot redeliver
an old message to a different chat. A lost HTTP response is safe to retry.

## Bounds and limitations

- Polling defaults to five minutes (configurable between 60 and 3,600 seconds).
- This MVP searches `in:inbox from:<exact sender> newer_than:7d`. Messages moved
  out of the inbox before a poll and outages exceeding seven days may be missed.
  Gmail push/history synchronization is the later upgrade for broader coverage.
- Each complete scan supports at most 1,000 candidates over 20 pages. An
  incomplete scan fails without establishing a baseline or queueing partial
  results. Narrow a high-volume sender rule if the limit is reached.
- Exact sender matching is routing, not proof that an email is trustworthy.
  Email content cannot choose a task, destination, or approval value.
- Failed worker tasks remain visible in the bot chat. They are not automatically
  rerun; review the failure rather than repeatedly generating copies.
- Results appear when Chat fetches the conversation list or conversation. This
  milestone does not add live polling or an unread indicator to an already-open
  page.
- The initial baseline is intentionally quiet. A first live test must send a new
  matching email after the baseline log appears.

## Acceptance check

1. Choose one sender and one existing bot conversation; enable that rule.
2. Wait for `baseline=True queued=0` in the watcher log.
3. Close Kyrex Chat and send a new matching email.
4. After a poll, reopen the destination chat and confirm the readable email.
5. Restart the watcher and confirm that the same email is not queued again.
6. Pause the destination bot: a new matching email must not be redirected.
7. Resume within the seven-day window: it should queue on the next scan.

Stop with `docker compose -f automation/docker-compose.yml stop email-watcher`.
Disable the Railway flag or rotate the credential to revoke the service.
Do not use `down -v` unless intentionally deleting the baseline/receipt state.
