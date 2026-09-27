# Browser Host on a Railway Sandbox VM

This is the Hobby-plan migration path for the Google Messages `#L6Workout`
watcher. Railway's Sandbox runs a Docker daemon, so the existing agent and
manual viewer images can run without changing their task or approval logic.
The Sandbox is a **long-running, billable VM**: create it with its idle timeout
disabled and check Railway's usage limits. The browser profile lives on the
VM's disk. Destruction loses that disk unless you have saved a checkpoint.

## Create the VM

From a PC with the Railway CLI, sign in and link the **same project and
environment** as Kyrex Cloud. The CLI must be recent enough to support
Sandboxes, private networking and port forwarding:

```sh
railway login
railway link
railway sandbox create --private-network --idle-timeout-minutes 0
railway sandbox ssh
```

The CLI selects the newly created Sandbox as active. Add an SSH public key to
your Railway account if SSH or forwarding asks for one. Do not add a public
domain or publicly forward the viewer. The VM's PRIVATE setting lets the VM
reach the Cloud service over Railway's internal network; the Docker agent
overlay uses the VM's host network for that route. First use the already
verified direct Railway public URL below; after verifying internal DNS and the
Cloud service's listening port, the WebSocket address may be switched to
`ws://<cloud-service>.railway.internal:<port>/api/browser-hosts/ws`.

In the new VM's SSH session:

```sh
git clone https://github.com/kp84-hub/kyrex.git ~/kyrex
cd ~/kyrex
sudo bash browser-host/railway_sandbox.sh init
```

`init` requires `docker compose`, builds both images, and creates the shared
browser-profile directory for uid 1000. If the Sandbox has Docker but not the
Compose plugin, install the Docker Compose plugin there before `init`. The
Sandbox images keep the original viewer's Chrome sandbox and loopback-only
VNC configuration. `docker-compose.sandbox.yml` runs the agent under the same
uid as the viewer to keep profile locks writable, with its private X display
on `:98` (viewer uses `:99`). Nothing publishes CDP or a VNC port.

## Enroll a new host and pair Messages

Enroll a **new** host in Kyrex (for example `railway-messages-01`) and copy
the one-time enrollment secret securely into the VM. Bind the intended
Calendar Bot to that exact new host. Keep `ovh-ny-01` operational until the
new host and one trigger work. Do not reuse the old host ID while both agents
are running.

Create `~/kyrex/browser-host/enrollment.env` in the VM with mode 0600. It is
git-ignored. Set these six variables, with the enrollment secret from Kyrex
and the **fixed** Google Messages conversation URL from your paired account:

```text
KYREX_HOST_ID=railway-messages-01
KYREX_HOST_OWNER=kp84-hub
KYREX_HOST_ENROLLMENT_SECRET=<one-time secret>
KYREX_HOST_CLOUD_URL=wss://kyrex-production.up.railway.app/api/browser-hosts/ws
KYREX_HOST_ALLOWLIST=messages.google.com,facebook.com,www.facebook.com
KYREX_GOOGLE_MESSAGES_CONVERSATION_URL=https://messages.google.com/web/conversations/<your conversation id>
KYREX_MESSAGES_BOT_ID=google-messages
```

The allowlist must also cover any sites needed by the existing Calendar Bot's
Level 6 browser task. Edit the file directly inside the VM, do not paste the
secret into chat or shell command arguments, then run
`chmod 600 browser-host/enrollment.env`.

Start a manual viewer **before** starting the agent:

```sh
cd ~/kyrex
sudo bash browser-host/railway_sandbox.sh password
sudo bash browser-host/railway_sandbox.sh viewer-start kp84-hub google-messages
```

In a separate terminal on your PC (still linked to the Railway project), run
`railway sandbox forward 6080`, then visit
`http://127.0.0.1:6080/vnc.html` on **that PC**. Enter the VNC password and
pair Google Messages with your phone. This viewer is available only through
Railway's authenticated SSH port forward; it has no public domain. This does
mean your viewer traffic passes through Railway's SSH infrastructure rather
than the previous VPS/Tailscale route. The existing viewer's per-profile lock
and one-hour maximum session duration still apply.

After you see the fixed conversation, return to the Sandbox SSH session:

```sh
sudo bash browser-host/railway_sandbox.sh viewer-stop
sudo bash browser-host/railway_sandbox.sh agent-start
sudo bash browser-host/railway_sandbox.sh status
docker logs --tail 40 browser-host-agent-1
```

The host must show **online**, and the agent must show `monitoring fixed
conversation`. Send a **new** `#L6Workout` in the fixed group from your phone,
then verify that Kyrex queued the task and posted its reply. Do not assume
moving machines fixes Cloud task admission: the earlier VPS watcher reached
the direct Railway endpoint, but the trigger did not queue; confirm the
Calendar Bot is running, granted and bound exclusively to the new host.
Only after a successful end-to-end test should you stop the old VPS agent.

## Preserve and restore the pairing

The running VM retains its disk. A **checkpoint** can retain its paired
Chrome profile and Docker images if the VM is later destroyed. To prepare a
consistent snapshot, stop both the agent and viewer first:

```sh
cd ~/kyrex
sudo bash browser-host/railway_sandbox.sh checkpoint-ready
```

Then capture a named checkpoint in Railway's **Sandboxes** dashboard. It
contains login cookies, the enrollment secret and the VNC password; restrict
access and do not share/export it. A VM started from a checkpoint needs
`--idle-timeout-minutes 0 --private-network` again, because those settings
are not inherited. Files are restored, but running processes are not. In
the restored VM run `sudo bash browser-host/railway_sandbox.sh agent-start`
and confirm the profile is still paired before sending a trigger. Do not run
both the old VM and its restored copy with the same host ID at once.

To stop future charges, stop the agent and explicitly destroy the Sandbox.
Take a checkpoint first if you need the browser login later.
