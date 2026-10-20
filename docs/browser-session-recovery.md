# Browser sign-in persistence and private reconnect

Browser sign-in is an account connection, separate from a task and from the
chat model. Closing a task should not discard a session cookie. A website may
still expire or revoke a login; Kyrex cannot extend that website's session.

## What changes

Both the private sign-in browser and managed task browser checkpoint session
cookies to their existing owner/Bot profile. The next browser restores missing
session cookies before navigation. Native expiring cookies and localStorage
remain Chrome's responsibility. A newer cookie already in Chrome wins. Cookie
partition keys are retained, so one embedded top-level site is not another.

The viewer now has a local Playwright controller over an anonymous pipe. It
retains the user-namespace sandbox and has **no CDP TCP listener**. This is a
deliberate change from the former unmanaged viewer: it makes the initial human
sign-in checkpointable before the browser closes. It also aligns container
cookie-store and partitioning behavior with the task browser. Screen, keyboard,
password and cookie traffic do not pass through Railway or the model.

Task browsers continue to close per task, preserving their existing network
interception lifetime. This implementation does not introduce a resident browser
pool or weaken booking-write interception to keep a browser alive.

For an existing non-root-owned profile, the agent launches its operator worker
with that profile owner's UID, primary group and home, and clears supplementary
groups. The agent retains its authenticated connection and exclusive profile
lock. Chrome's atomic Local State/Preferences writes therefore stay readable
by the private viewer. Root-only legacy profiles retain their previous worker
identity; unrelated profiles are not migrated. An explicit artifact workspace
is preserved; otherwise the worker uses a private profile-local artifact folder.
The operator does not receive the agent enrollment secret or viewer password.

The checkpoint and its local key live inside the profile, at 0600. The
checkpoint is Fernet encrypted and bound to that profile's canonical path. The
key is on the same host/profile volume: full access to that volume remains
access to its logins. Protect backups as credentials. Never send either file
to Cloud or check them into Git. Corrupt or missing-key checkpoints stop
automation; no alternate profile or account is selected. Future-dated or
older-than-seven-day checkpoints are ignored. Explicit human reconnect can
replace an unusable recovery file/key while retaining Chrome's native profile,
so fresh sign-in remains possible. Successful observed logout writes an empty checkpoint. Resetting a
profile removes its key and checkpoint too. A crash between cookie deletion
and the next checkpoint can retain the prior snapshot; server-side revocation
still applies. Do not use checkpointing as a replacement for provider logout.

## Reconnect from Connections

After the one-time host setup below:

1. Open **Connections → Browser**, select your Browser Bot and enter the page.
2. Select **Reconnect sign-in**. Once ready, select **Open private sign-in browser**.
3. Connect Tailscale on that device, use the existing viewer password, and sign
   in to the website yourself.
4. Select **Finish sign-in and hand back** in Connections. The viewer saves the
   checkpoint, closes Chrome and releases the same profile lock automation uses.
5. Select **Test browser connection** to verify readiness, then ask for the
   original task. Reconnecting does not submit or replay a booking.

The control request names the authenticated owner, selected Bot, enrolled host,
website and exact window lease. It is a UI control RPC, not a model tool. The
non-root host supervisor only launches the fixed, existing `viewer_ctl.py hold`
command. It gets no Docker socket. It refuses a busy profile/host, retains the
45-minute viewer TTL, claims commands before acting, and never replays an
uncertain command after restart. Status reads do not start anything. Hand-back
cannot end an unrelated manually opened viewer. The mailbox holds only bounded
control metadata on the profile volume; it is not a second agent task queue.

## One-time host rollout

Deploy Cloud and Chat through their existing pipeline. On the Browser Host,
end the previous viewer before rebuilding its image. Existing sign-ins and
profiles must not be deleted. The previous native viewer cannot retroactively
checkpoint a session cookie it has already lost; the first sign-in through the
new viewer may be needed once.

Use the existing VNC password file and the existing owner-only Tailscale Serve
configuration. Obtain its HTTPS URL from `tailscale serve status`; do not expose
the viewer publicly. The configured URL must be an HTTPS `*.ts.net` URL, without
credentials, query parameters or a nonstandard port. `/vnc.html` is appended to
the origin if omitted. The app links directly to this private endpoint; it
does not proxy the viewer.

From the existing checkout (substitute the actual private URL):

```bash
cd ~/kyrex
python3 browser-host/viewer_ctl.py end
git pull --ff-only

# The existing profiles root can be root-owned. Provision only this control
# directory for the UID 1000 supervisor; leave browser profile ownership intact.
sudo install -d -o 1000 -g 1000 -m 0700 \
  "$PWD/browser-host/profiles/.kyrex-browser-reconnect"

export KYREX_VIEWER_VNC_PASSWORD_FILE="$PWD/browser-host/viewer-vnc-pass"
export KYREX_VIEWER_OWNER=unused
export KYREX_VIEWER_BOT=unused
export KYREX_PRIVATE_VIEWER_URL='https://YOUR-VPS.YOUR-TAILNET.ts.net/vnc.html'

docker compose --env-file browser-host/enrollment.env \
  -f browser-host/docker-compose.viewer.yml --profile reconnect \
  up -d --build reconnect

KYREX_HOST_BUILD_SHA="$(git rev-parse HEAD)" docker compose \
  --env-file browser-host/enrollment.env \
  -f browser-host/docker-compose.cloud.yml --profile cloud \
  up -d --build agent
```

`unused` only satisfies Compose validation for the inactive legacy viewer;
the supervisor obtains its real enrolled owner from `KYREX_HOST_OWNER` in the
existing enrollment environment and takes the selected Bot from the
authenticated request. The supervisor refuses to start with no owner/private
URL. Its only listeners remain the existing loopback noVNC/VNC listeners while
a human window is active. Existing tailnet ACL and viewer-password requirements
still apply. No new public port or Chrome debugging endpoint is installed.

Checkpoints use the same profile filesystem ownership as the profile itself.
The viewer remains UID 1000; a privileged agent writer retains that profile
owner's access to new checkpoint files. Do not recursively change profile
permissions, copy logins between accounts, reset enrollment, or rebuild with
volume deletion. Normal service restarts retain the profile and checkpoint.

### Existing root-owned Chrome control files

An older root worker can leave `Local State` and `Default/Preferences` unreadable
to the UID 1000 viewer even when Cookies and the encrypted checkpoint are
readable. After rebuilding the agent with profile-owner workers, end the private
viewer and run this targeted repair inside the agent:

```bash
docker exec browser-host-agent-1 python3 /host/profile_identity.py \
  --owner YOUR_ENROLLED_OWNER --bot YOUR_BROWSER_BOT
```

The command checks the enrolled owner and takes the existing exclusive profile
lock. It changes only root-owned instances of those two regular, single-link
files to the existing non-root profile owner's UID/group; contents and modes
are retained. Symlinks, hard links and unrelated owners are refused. It does not
change other browser profiles, cookies or localStorage, and does not create a
new login. Verify the existing session; a site-revoked login still needs a
human sign-in through the repaired viewer.

## Validation boundaries

Unit tests exercise real Fernet/private files, owner/Bot/partition isolation,
logout, reset, corrupt/expired checkpoints, control authorization and
non-replay. The browser fixture signs in to an intercepted/local test site,
closes the actual private-browser helper, and reopens the profile through
managed tasks. Its test-only browser arguments accommodate CI's sandbox
restrictions; production sandbox arguments are checked separately. API tests
drive authenticated routes and the real host-channel control exchange. UI
tests exercise reconnect/hand-back and lost acknowledgements with mocked HTTP.

These fixtures do not prove a live Glofox login remains valid for any particular
period. Verify the deployed host once: sign in through Connections, hand back,
pass a diagnostic, repeat it, then restart the agent and repeat it again. A
booking still needs its existing exact-class review and one-time confirmation.
