# Git authentication in the local TUI

The local TUI, Kyrex Chat's repository executor, and a separately connected
assistant do not share an authentication session. A successful repository read
through one surface is not evidence that Git in another surface can fetch.

## What the runner does today

The Go launcher inherits the host environment and sets the engine's working
directory to an isolated Rift copy. The command runner has two execution modes:

| Mode | Network and credential behavior |
| --- | --- |
| Working bwrap | `--unshare-all` isolates the network. Only system directories and the workspace are bound; the user's home credential files and credential-helper dependencies are not generally available. |
| bwrap unavailable | Commands run with the existing fallback policy. GitHub token variables and other secrets are removed from their environment. A Git credential helper or a repository-local credential can still be consulted. |

Neither mode promises that authenticated network Git commands work. An
environment variable named `GITHUB_TOKEN` also does not, by itself, configure
Git's HTTPS authentication. Do not expose tokens or disable sandboxing to make
arbitrary shell commands authenticate.

## Interpreting a failure

Nonzero command exits are errors, with the original exit code and output
preserved. Git authentication errors include bounded recovery guidance and
whether sandboxing and GitHub environment filtering were active. This metadata
contains booleans, never credential values or credential-file contents. The
TUI also treats nonzero exit codes from older engines as failed tool events.

Compare the same operation in the owner's normal terminal and the TUI:

- If both reject authentication, inspect the owner's GitHub login and repo
  permissions. The error alone does not prove that a token expired.
- If the host succeeds and the TUI fails, inspect the runner mode, network
  isolation, and helper availability. Repeating login inside the runner is not
  a durable remedy for missing access to the host's credential helper.
- A successful cached `git rev-list` or `git log` after a failed fetch does not
  establish current remote state. Sequence dependent shell commands with `&&`.
- Preserve local work. Do not rebase or publish against a base described as
  current until a fetch actually succeeds. Do not silently switch to a patch
  transfer as an authentication repair.

The diagnostics fix does not repair a rejected host credential, add a local
GitHub login flow, or add an authenticated host-mediated fetch/push adapter.
That adapter needs explicit local account/workspace authorization and the
existing effect/approval boundaries; it must not reuse Chat's read-only GitHub
connection as write authority. Regression tests use controlled subprocess
responses and a local shell, not the owner's live GitHub account.
