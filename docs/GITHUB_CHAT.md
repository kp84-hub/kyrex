# GitHub reads in Kyrex Chat

After deploying this change, open **Connections → GitHub → Connect**.
Create a fine-grained personal access token in GitHub, choose the resource owner
and selected repositories, and grant **Contents: Read-only** (Metadata read is
implicit). Leave other permissions unset. Enter the token in the password field
and the allowed repositories as `owner/name`. Do not paste the token into Chat.

Kyrex verifies each repository and Contents access before replacing a connection.
The token is encrypted using the existing `WEB_SESSION_SECRET` (or
`KYREX_PROVIDER_SECRETS_KEY`) and saved on the Cloud data volume. No new server
secret is required when Calendar/Gmail credential storage already works.
Keep that encryption key stable across deployments. Disconnect removes Kyrex's
stored copy; revoke the token in GitHub to invalidate it at the provider.

The Overwatcher and Developer preset already grant `repo:read`; this policy now
exposes the `github_read` Chat tool. Bots without that grant cannot use it.
The tool checks the authenticated Chat owner's connection, never an owner supplied
by the model. It can check status, list selected repositories, list directories,
and read UTF-8 files at a branch or commit. Private access is limited both by the
GitHub token and Kyrex's repository allowlist. It exposes no GitHub writes.

Example: **“List the GitHub repositories I connected, then read the README in
kp84-hub/kyrex and summarize it.”** Explicit GitHub read questions in a Developer
Bot chat use the read engine; requests to change code retain the existing coding
executor and approval flow. This does not clone repos or add remote GitHub access
to the separate headless coding executor. GitHub reads should be performed directly
in Chat, rather than delegated as a local coding task.

Responses are bounded: 200 directory entries and 40,000 characters per text file,
with explicit truncation flags; binary files, symlinks, submodules and files over
256 KB are rejected. GitHub errors are sanitized; revoked tokens need reconnecting
and rate limits require waiting. Connection status indicates stored credentials;
a successful Contents read verifies current provider access. Repository content is
untrusted input and must not be followed as instructions.
