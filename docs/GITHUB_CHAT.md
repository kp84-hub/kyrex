# Connect GitHub in Kyrex Chat

Open **Connections → GitHub → Connect**. Continue on GitHub, choose the
repositories Kyrex may read, and authorize the connection. Return to Kyrex;
the Connected status appears after the server has verified the authorization.
If your phone blocks the new tab, use the displayed **Open the sign-in page** link.

For the first connection, GitHub first asks you to create a preconfigured
**Kyrex Repository Reader** app. Confirm its name, then choose repositories.
Kyrex supplies the read permissions and callback addresses through GitHub's app
manifest flow, so there are no tokens, repository names, private keys, client
secrets or Railway settings to copy. The app is owned by your GitHub account.
Subsequent connections reuse it. This app is separate from Kyrex's existing
GitHub OAuth app used for signing into the product.

Use the same GitHub account you use to sign into Kyrex. The app requests only
**Contents: Read-only** and **Metadata: Read-only**. Use **Manage repositories**
to change the selection on GitHub. Kyrex checks the live user installation and
repository list on every repository read; removed or suspended installations
cannot continue reading via a saved repository list.

App client configuration, access tokens, refresh tokens and the PKCE verifier
are encrypted on the existing durable Cloud volume using `WEB_SESSION_SECRET`
(or `KYREX_PROVIDER_SECRETS_KEY`). Keep that key stable across deployments.
The manifest's app private key and webhook secret are discarded; no app-wide
installation token is used. Expiring user tokens refresh automatically while
the refresh grant remains valid. A revoked/expired grant requires reconnecting.

OAuth and registration states are random, durable, expire after 15 minutes,
and are bound to the signed-in owner, callback origin and handoff step.
Callbacks consume them once. The installation id from the browser is only a
hint; the authorized GitHub API must confirm it belongs to this user and app.
Disconnect removes stored reading credentials and cancels pending handoffs,
while preserving app registration for a future connection. Remove the app in
GitHub if you also want to uninstall it; disconnect does not uninstall it.

The Overwatcher and Developer presets already grant `repo:read`, which exposes
the `github_read` Chat tool. Other Bots need that permission. The model and
engine never receive credentials. There are no GitHub push, merge, creation or
delete operations. The separate headless coding executor does not gain remote
GitHub access: private repository reads run directly in Chat.

Test: **“List the GitHub repositories I connected, then read the README in
kp84-hub/kyrex and summarize it.”**

Limits: 500 selected repositories per installation, 100 user installations,
200 directory entries per read, 256 KB per UTF-8 file, and 40,000 returned file
characters with explicit truncation flags. Binary files, symlinks and submodules
are unsupported. Repository contents remain untrusted data, never instructions.
Existing previously connected fine-grained tokens keep working, but the Chat UI
no longer accepts or asks for manually pasted tokens.

The public HTTPS Chat address must stay stable. Callback origins use the existing
Chat host/public-base configuration used for login. Changing that address also
requires updating the reader app's registered callback/setup addresses on GitHub.
