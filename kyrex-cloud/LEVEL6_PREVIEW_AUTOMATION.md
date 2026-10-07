# Weekly L6 Besties preview

Enable these variables on the Kyrex Cloud service that runs the worker, then
redeploy:

```env
KYREX_LEVEL6_PREVIEW_SCHEDULE_ENABLED=1
KYREX_LEVEL6_PREVIEW_RECIPIENT=L6 Besties
WEB_ALLOWED_GITHUB_USERNAME=kp84-hub
```

The worker checks on Sunday evenings at 7 PM America/New_York. It queues one
read-only preview for the upcoming Monday–Saturday week, with durable
deduplication across restarts during the due hour. Daylight saving time follows
New York. The first run after enabling on October 7, 2026 is October 11 at 7 PM,
for the week starting October 12.

The running Browser Bot and Calendar Bot must belong to the configured owner
and share a Browser Host. The existing Facebook/Glofox validation supplies the
workouts, dates and trainers. If the upcoming post is unavailable or unreadable,
Chat shows the failure; the scheduler does not reuse last week's workouts. A
failed attempt is not automatically retried that week.

Results appear in the saved **L6 Besties · #L6Workout** Chat conversation. The
preview can remain there without expiring. Choose **Prepare for L6 Besties**
when ready: the linked phone resolves the existing group and verifies every
recipient. Review the exact message and recipients, then press **Send**. Opening
the preview or preparing it never sends a message. An expired or cancelled
phone preview can be prepared again; an accepted or unknown send is not retried.

The group must appear in the synced phone snapshot. If it is new, make sure it
contains a text message and sync the companion before preparing the send.

The preview scheduler does not require `KYREX_LEVEL6_SEND_ENABLED`. The old
`KYREX_LEVEL6_SCHEDULE_ENABLED=1` opt-in is accepted as a migration alias and now
schedules previews at the Sunday time. Explicit
`KYREX_LEVEL6_PREVIEW_SCHEDULE_ENABLED=0` disables this scheduler even if that
legacy flag is set. Manual legacy `#L6Workout` delivery commands retain their
existing separate switch; this scheduler never queues them.
