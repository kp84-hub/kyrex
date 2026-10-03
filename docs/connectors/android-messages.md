# Messages through Google Messages for web

The deployed automated pairing screen was rejected by Google with “This browser
or app may not be secure.” Do not repeatedly retry that sign-in or treat the
connector as verified. The next test uses the existing private manual viewer:
you sign in yourself in regular Chrome, close the viewer, then let the bounded
Messages reader verify that same saved profile. This manual path is not yet
verified with a live Google account.

This is Google device pairing, not Gmail OAuth. Installing Kyrex as a PWA does
not grant SMS inbox permission. Google no longer offers web QR pairing in the US.
The phone must remain online. Google permits only one active web computer,
so another Messages web session can interrupt this connector.

## Current reading scope

“Show my texts” or “Find my text messages about school” reads visible text
from up to ten recent conversations and ten loaded messages per conversation.
This is a bounded view, not a complete archive search. It includes text Google
Messages displays, including RCS text; attachments and sending are unsupported.
Opening conversations may mark them read in Google Messages.
No message links are opened and message content never becomes tool instructions.

The pair screen's controls stop operating once Messages is paired. Bots have no
access to the screen/input endpoints, arbitrary navigation, or a send operation.
Message text is returned directly to the authenticated owner's chat; normal
conversation retention applies. It is not copied into the old SMS snapshot DB.

## Deployment

Cloud and the Browser Host agent both require this update. Rebuild/restart the
existing host agent using the repository's `browser-host/docker-compose.cloud.yml`.
It already provides Chrome, Playwright, Xvfb and a persistent `/profiles` volume.
The image now also ships `messages_connector.py`; the authenticated handshake
advertises its availability. An old or offline agent is reported explicitly.
The automated reader adds no listener. The manual sign-in test below uses the
existing separate, Tailscale-only viewer service.

Google cookies remain in a dedicated owner-hashed profile under
`/profiles/connectors/messages/`, separate from Calendar/Browser Bot profiles.
The parent profile directory is private. Pairing screen/input travels transiently
over the existing authenticated TLS host channel and owner-authenticated API;
never through task history, provider prompts, or audit details.

Disconnect first revokes Cloud read consent, then erases the dedicated host
profile. If the host is offline, cleanup is reported pending and can be retried;
Google's Device pairing screen can independently revoke the paired computer.
Legacy phone-upload credentials/snapshots are also revoked by Disconnect.

## Verification limitation

Automated tests cover the channel, isolation, consent, revocation, bounded DOM
reading and UI flow. A live Google account must still verify sign-in/pairing on
the deployed host. Google can reject sign-in from an automated browser or change
its web DOM; the connector reports failure rather than claiming a connection.

## Manual Chrome test on the VPS

This is an operator test before simplifying Connect. It is not a bypass of
Google's security refusal: the manual viewer retains its sandbox and has no
Playwright, automation driver, CDP listener, altered user agent or fingerprint.
If Google rejects this browser too, stop and report that result.

Prerequisites: the owner-only Tailscale viewer from `browser-host/README.md`,
an existing mode-0600 `browser-host/viewer-vnc-pass` readable by uid 1000, and
no other owner currently using this host's viewer. Run from the repo root.
Use the exact `KYREX_HOST_OWNER` from enrollment for `<owner>`; it is an owner
identifier, not a password. Do not copy enrollment secrets into Chat.

1. Update the checkout, build the agent and viewer images, then stop the agent
   to close its automated Chrome and release the Messages profile. Stop the
   existing viewer too before transferring file ownership. The agent is offline
   during this test; other Browser Bot work must wait.

   ```bash
   export KYREX_VIEWER_OWNER='<owner>'
   export KYREX_VIEWER_BOT=messages-connector
   export KYREX_VIEWER_VNC_PASSWORD_FILE="$PWD/browser-host/viewer-vnc-pass"
   git pull --ff-only origin main
   docker compose --env-file browser-host/enrollment.env -f browser-host/docker-compose.cloud.yml --profile cloud build agent
   docker compose -f browser-host/docker-compose.viewer.yml build viewer
   KYREX_VIEWER_VNC_PASSWORD_FILE="$PWD/browser-host/viewer-vnc-pass" python3 browser-host/viewer_ctl.py end
   docker compose --env-file browser-host/enrollment.env -f browser-host/docker-compose.cloud.yml --profile cloud stop agent
   ```

2. Prepare only this connector's files for the non-root viewer. Root-created
   profile and lock files are mode 0700/0600; changing browser flags will not
   fix their ownership. Both containers must be stopped for this step.

   ```bash
   messages_key=$(python3 -c 'import hashlib,os; print(hashlib.sha256(os.environ["KYREX_VIEWER_OWNER"].encode()).hexdigest())')
   messages_profile="$PWD/browser-host/profiles/connectors/messages/$messages_key"
   messages_state="$PWD/browser-host/profiles/.kyrex-viewer-state"
   sudo install -d -m 700 -o 1000 -g 1000 "$messages_profile" "$messages_state"
   sudo install -d -m 700 -o 1000 -g 1000 "$messages_state/locks" "$messages_state/records"
   sudo chown -R --no-dereference 1000:1000 "$messages_profile"
   sudo touch "$messages_state/locks/${messages_key}__messages-connector.lock"
   sudo chown 1000:1000 "$messages_state/locks/${messages_key}__messages-connector.lock"
   sudo chmod 600 "$messages_state/locks/${messages_key}__messages-connector.lock"
   python3 browser-host/viewer_ctl.py start --owner "$KYREX_VIEWER_OWNER" --connector messages --ttl 2700
   tailscale serve status
   ```

   Open your existing private Tailscale viewer URL on your phone. Chrome opens
   Google Messages with this owner's dedicated connector profile. Sign in
   yourself and confirm pairing on your phone. Closing the browser tab on the
   phone does not end the viewer: use the end command below. The viewer also
   expires after 45 minutes. No Google password is sent through Kyrex Cloud.

3. End manual control and restart the updated agent.

   ```bash
   python3 browser-host/viewer_ctl.py end
   docker compose --env-file browser-host/enrollment.env -f browser-host/docker-compose.cloud.yml --profile cloud up -d agent
   ```

   Return to Connections → Messages → Connect. The reader opens the same profile.
   If it verifies the conversation list, tap Finish connecting; Cloud read consent
   is still required. If it redirects to sign-in or fails verification, report
   that result rather than entering the password through the automated screen.
   Only after this test succeeds should we expand the browser handoff UI.

Manual and automated sessions share one exclusive lock, including Disconnect
cleanup. If Disconnect is requested during manual control, Cloud consent is
revoked immediately and profile cleanup remains pending until the viewer ends.
Repeat Disconnect afterward to finish local cleanup.
