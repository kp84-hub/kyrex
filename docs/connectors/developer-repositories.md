# Developer Bot repositories

Developer Bot can inspect every repository selected in the GitHub reader
connection. Editing a different project requires a local checkout: choose it
under **Bots → Developer Bot → Repository**. Repository controls stay in Bot
Settings and do not appear in the chat transcript or composer. Kyrex authenticates the clone with
that owner's reader connection and stores each Bot/repository checkout separately.
It preserves the previous Rift, dirty files, index and branches. Choose **Original
workspace** to return to the original project. Re-selecting a repository resumes
its existing work; it does not reset or silently pull over local changes.

Managed checkouts now fetch their actual default branch with the owner's reader
connection before each task. A clean branch advances only by fast-forward;
tracked edits, divergent commits, and untracked or ignored file collisions are
preserved. The engine receives freshness metadata instead of Git credentials
and does not perform a second anonymous fetch. Original workspaces retain their
existing local workflow; select the connected repository to use this managed path.

While the Bot is paused and idle, **Refresh checkout** performs the same check.
The panel shows local commits ahead/behind the last fetched base. If local work
prevents an update, ask Developer Bot to inspect and synchronize that work;
Kyrex does not reset, discard, auto-stash or automatically rebase it.

The Repository control follows the owner's Bot's effective `fs:read`,
`repo:read`, `fs:write`, and `repo:pr` grants, including older coding policies
with wildcard rules or extra rules. It does not depend on the Bot's display
name or its legacy role label. Missing or denied grants keep the control and
repository API unavailable; no Bot policy is changed automatically.

Finish or stop the current task, open **Bots → Developer Bot → Repository**, click **Pause Bot**, choose
the project and click **Use repository**, then **Start Bot** in the same panel.
Queued/running tasks also prevent switching on the server. Request edits and run tests in that project. The
runtime identifies the selected repository in the engine context. It checks live
reader access before execution and rejects a durable task whose Rift changed
since submission. `repo:` uses the older publishing workflow and is refused for
these managed checkouts; use a normal Developer Bot request.

## Publishing

The reader stays read-only. **Enable PR publishing** opens a separate GitHub App
consent flow asking for Contents write, Pull requests write and Metadata read.
Select only repositories in which you want Kyrex to create review branches and
PRs. Reader and publisher installations, encrypted credentials, callback states
and repository selections remain separate. **Manage publishing repositories**
changes that selection; **Disconnect publisher** revokes Kyrex's local publisher
connection without removing reader access. GitHub's installation settings can
also remove access.

After Developer Bot finishes edits/tests:

1. Pause the Bot and wait for its tasks to finish.
2. Open **Repository → Review changes** and inspect the diff.
3. Enter a PR title and a description including actual test results.
4. Click **Create pull request** and open the returned GitHub link.

The host rebuilds the file snapshot and checks it against the reviewed tree
before publishing. It checks the current owner's publisher installation and
repository selection again. It writes blobs, a commit and a new `kyrex/review-*`
branch through GitHub's Git database API, then opens a PR against the checkout's
original default branch. It does not push to the default branch or merge.
Changing files after review requires a new review. The real index, local commits
and dirty files are untouched.

Review and publication also fetch and verify the latest base. A stale or
divergent checkout cannot publish, and a changed base invalidates an earlier
review even when its file tree is identical. Review identifiers bind the owner,
Bot, repository, workspace, base commit and reviewed files. Reviews made before
this update need to be opened again.

A repeated publish of the same confirmed snapshot returns its existing PR link.
A failed or interrupted publish is unconfirmed and is not retried automatically;
check GitHub for a branch/PR first. Source stays in the checkout for manual recovery.
Publishing limits: 100 changed files, 5 MB of changed file content, and
1,000,000 bytes of diff. Review returns the full diff within that bound; it
never truncates the reviewed snapshot to fit. Larger diffs report the actual
byte count and limit and require a manual Git workflow.
Binary blobs and ordinary executable files are supported. Workflow changes,
symlink changes and submodule changes require a manual Git workflow.

## Host boundaries and deployment

GitHub credentials stay in the Cloud host. Clone credentials are temporary Git
process environment headers, absent from command arguments, remotes, engine
payloads and logs. Publishing credentials never enter an engine process. GitHub
OAuth state is owner/origin/step-bound; tokens remain encrypted. API routes accept
repository names and reviewed tree identifiers, never caller paths or tokens.

Workspaces and SQLite metadata live under the existing persistent
`KYREX_DATA_DIR`; the Cloud web and worker processes share it. Deploy the Cloud
web/worker and Chat frontend together. No Browser Host or Android update is
needed. Existing installations do not acquire write permissions automatically:
the owner must complete the separate publisher consent after deployment.

The deployed developer runtime includes Node 22 with npm, Go 1.26, a C/C++
compiler, pytest, pytest-asyncio, pytest-mock and pre-commit. Project-specific
dependencies still need their normal installation in the selected checkout.
The runtime workflow builds both Cloud Dockerfile targets and executes small
Python, npm and Go suites in the resulting containers with networking disabled.
This does not claim that every project, native desktop target or external
integration can build on this Linux host.
