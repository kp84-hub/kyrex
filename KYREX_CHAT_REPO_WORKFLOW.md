# Kyrex Chat repository workflow

## Current path

Jev selects a Bot route. The Overwatcher can delegate to a Developer Bot. The Developer
Bot submits a durable task, `git_workflow.py` runs the Kyrex engine in its
persistent Rift, and Chat shows task events and the final result. A separate
Firestore memory holds only facts the owner explicitly saved. Other Chat
conversations and external assistant threads are not shared with the Bot.

The repo executor currently auto-approves file edits, commits and pushes
changes, and normally opens a PR. Its self-review checks whether the diff
matches the task, but an unavailable review does not block a PR. A persistent
Rift also retains earlier committed and uncommitted work. These are workflow
decisions, not things a stronger system prompt can fix.

## Desired experience

1. **Orient.** Show the owner which repo, base commit, branch, uncommitted
   changes, and active task are in play. Verify remote state before a coding
   task. Jev still decides the Bot route; Kyrex chooses how to inspect and act.
2. **Work together.** Maintain one isolated draft worktree per owner, Bot, and
   conversation. A follow-up edits that draft. A new conversation starts from
   a fresh base and never picks up another conversation's uncommitted files.
   Preserve drafts after restarts and expose a way to resume or discard them.
3. **Show progress.** Stream meaningful stages, changed files, test results,
   and recoverable failures into Chat. A dropped phone connection reattaches
   to the durable task instead of silently starting a second one.
4. **Review before publishing.** Show a bounded diff and verification summary.
   Allow the owner to request further edits to the same draft. Require an
   explicit owner action for PR creation when that preference is enabled;
   require a separate explicit action for merge. Do not let model text stand
   in for those actions. Keep existing host policy and approval checks.
5. **Carry context.** Store a bounded, owner-scoped project handoff containing
   links and verified commit/PR/task identifiers, updated from real task
   outcomes. Treat saved personal facts and handoff notes as hints; refresh
   GitHub/repo status before claiming something is current. Keep private
   messages, credentials, browser images, and local paths out of public git.

## First reliability slice

This change makes persistent-Rift base fetches authoritative: a failed fetch
stops before editing, a clean checkout behind the remote base fast-forwards,
and a dirty-behind or diverged checkout stops with a useful explanation while
preserving local work. Chat also receives named stages for repo preparation,
agent work, commit, review, and PR creation.

Developer progress now includes bounded pre-tool commentary and named stages
from actual tool events. Chat and the Overwatcher's delegated-work card show
one current update with a collapsed activity history. Cards continue refreshing
while the coordinator replies; transcript reconciliation waits until that turn
finishes. A status follow-up gives the coordinator the latest safe update,
without replaying the activity history. Tool arguments, command output, and
reasoning are excluded from the progress projection. A finished command is not
evidence that tests passed. Long developer results have an excerpt plus an
expandable full result; the stored response is unchanged.

This does not yet provide isolated drafts, shared project handoffs, or a
review-before-PR setting. Those need their own data model, owner controls,
and end-to-end tests before Kyrex Chat can replace the current workflow.
