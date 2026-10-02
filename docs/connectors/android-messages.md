# Messages through Google Messages for web

In Connections, tap **Connect**. Kyrex opens a private Google Messages screen.
Sign in with the Google account used by Messages on the phone, then confirm
the matching emoji in Google Messages → Device pairing. Tap **Finish connecting**
and return to Connections. There are no phone scripts or manual sync commands.

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
No additional listener, VNC port, app, or service is needed.

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
