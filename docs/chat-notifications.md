# Chat notifications

The header bell opens an owner-scoped inbox of saved task results, failed or
stopped work, pending task approvals, and visible trainer-monitor alerts.
Read state persists across reloads and devices. Clicking an alert opens its
conversation; it never approves, retries, sends, or executes the task.

To receive system notifications, open the bell on each phone or PC, choose
**Enable device notifications**, and allow the browser prompt. Then choose
**Send test notification**. Test queued means the delivery is pending, not that
the device has received it. Supported browser push services are Chrome/FCM,
Firefox, Apple Web Push, and Edge/WNS. Browsers or embedded views without Push
support still have the inbox; the installed Android Chat host uses the browser's
TWA support. Actual delivery depends on browser/OS notification permissions and
background restrictions. On iOS/iPadOS, Web Push requires an installed Home
Screen web app; see [Apple's Web Push guide](https://developer.apple.com/documentation/usernotifications/sending-web-push-notifications-in-web-apps-and-browsers).

Device notifications contain generic text and an opaque inbox identity. Email,
health, task payloads and approval tokens are not copied to the notification.
Opening it still requires the correct signed-in account. **Disable on this
device** revokes its server subscription before attempting browser cleanup.
Signing out also revokes subscriptions bound to that browser session. Ordinary
cookie expiry does not erase device consent; re-authentication to the same
account preserves it. A notification already handed to the browser's push
service can still arrive after revocation, with generic content only.

## Deployment

Both Cloud Dockerfiles install pinned `pywebpush==2.1.2`. Use HTTPS and persist
`KYREX_DATA_DIR` with a stable `WEB_SESSION_SECRET` (or
`KYREX_PROVIDER_SECRETS_KEY` for notification encryption). The VAPID signing key
is generated once, encrypted in `chat_notifications.sqlite3`, and reused after
restarts. No third-party notification account, Firebase app setup, or manual
VAPID-key generation is required. `KYREX_WEB_PUSH_SUBJECT` can optionally set
the operator's HTTPS or `mailto:` contact; otherwise registration uses the
same-origin public app address. Changing the encryption secret makes saved
subscriptions/keys unreadable; devices need to be enabled again after recovery.

The web lifespan observer consumes existing CloudTaskStore events. Its cursor,
inbox, and notification delivery claims persist on the same Cloud volume.
First deployment starts at the current task cursor, avoiding a historical
notification flood. Later restarts catch up from the saved cursor. It checks
the task owner, Bot binding and saved conversation before projecting anything;
delegated work links back to the coordinator's verified parent conversation.
It does not create or execute tasks. Trainer alerts consume only already-saved,
chat-visible changes; no new website reads or group sends occur.

Notifications describe the observed task transition, not proof of an external
write. “Task finished” can still require checking the saved result's unknown
outcome. Ordinary inline Chat replies and feature-specific review stores that
do not emit durable task events are not all notification sources in this
initial implementation. New automation adapters should publish through the
existing task/event lifecycle, or explicitly project a saved alert.

Delivery uses encrypted Web Push over fixed browser-service host families,
with redirects disabled, bounded timeouts and no raw provider error logging.
Claims recover after a process crash; only alerts are retried (up to four
attempts). Stable notification tags coalesce repeated delivery at the browser.
Expired subscriptions (404/410) are removed. Delivery is best-effort, not an
exactly-once guarantee. Up to ten devices per owner are supported; inbox
history is retained for 30 days, with the latest 100 shown. Alerts more than
24 hours old are not newly delivered to a device, though they remain in Chat.

## Verification

The notification workflow checks two owners, enrollment/revocation, cookie
expiry and login rotation, durable replay, concurrent claims, retry isolation,
endpoint restrictions, real VAPID signing and payload encryption without an
external push service. Chat recovery checks consent/denial, badge/read state,
deep links, stale authentication and service-worker behavior. Chromium checks
the shipped UI and an actual worker Push event while Chat is not loaded,
including a real generic notification. Provider-to-device delivery and the
user's Android/Windows notification settings still need a live test after
deployment.
