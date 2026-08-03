# Kyrex Cloud

A headless, unattended surface for Kyrex — trigger a task from Telegram, it runs the real engine in an isolated cloud container with no human approving edits live, and reports back with a pull request. See the main [README](../README.md#kyrex-cloud-telegram) for what it actually does.

This is a from-scratch deployment guide, not a description of a hosted service — there is no shared instance to sign up for. Everything below stands up your own copy, with your own credentials, on your own infrastructure.

## Architecture

Five pieces, each doing one job:

### 1. The core trick — reuse the VS Code bridge (headless_agent.py)

The whole cloud path hinges on the fact that Kyrex already talks to its VS Code extension over an NDJSON stdio protocol via kyrex_engine/core_bridge.py. The cloud agent reuses that exact bridge instead of writing a second engine interface — one source of truth for "how we talk to the engine."

HeadlessAgent:
- Spawns core_bridge.py into a subprocess with KYREX_VSCODE=1, WORKSPACE_ROOT, and PROJECT_SOURCE_ROOT set to the target repo.
- Waits for the startup handshake (phase:IDLE) before sending the task — doubles as a config/API-key check.
- Sends the task as {"type":"chat","content":task}.
- Auto-approves everything the engine asks for: propose_edit → edit_decision (accepted), confirm_request (deletions) → confirm_response (approved).
- Waits for chat_done, then collects the final response, tool calls, and errors into a JSON summary. (Run standalone via its own CLI, this also includes a git diff of what changed on disk; driven by git_workflow.py, the diff is instead computed separately against the real branch — see below.)

Fully unattended: no GUI, no human approval step.

### 2. Real git workflow (git_workflow.py)

Wraps the bridge with an actual development loop:
1. Isolated working copy — a git worktree add off a local clone, or a fresh git clone from a URL; always cut from the latest fetched base branch.
2. Run the task through HeadlessAgent.
3. If anything changed: git add -A, commit (as "Kyrex Cloud Agent"), push the branch.
4. Open a real PR via the GitHub REST API (gracefully skipped if no token).
5. Result JSON written outside the repo — avoids a prior run's result file getting swallowed into the next run's diff.

Plus: a self-review pass (asks a model whether the diff actually matches the task — fails open, a broken review never blocks a real PR), repo aliasing (alias: task targets a different repo), and GIT_TERMINAL_PROMPT=0 so git fails loudly instead of hanging on a credential prompt with no TTY to answer it.

### 3. Telegram trigger (telegram_bot.py)

The user-facing entrypoint:
- Long-polls the Telegram Bot API for messages.
- Security: only acts on TELEGRAM_ALLOWED_CHAT_ID — everyone else silently ignored; one task at a time; discards any update backlog on restart.
- Runs each allowed message through git_workflow.py in fresh-clone mode (no local checkout exists on a cloud host).
- Streams live progress — edits the initial "⏳ Starting" message in place as git_workflow.py reports what it's doing.
- Formats the final reply from a structured result — status, self-review verdict, PR link — rather than dumping raw output.

### 4. Container (Dockerfile)

A python:3.11-slim image with git, ca-certificates, and bubblewrap, running python3 kyrex-cloud/telegram_bot.py as the container command. Build context must be the repo root, not kyrex-cloud/ — the image needs kyrex_engine/ alongside it as a sibling directory.

### 5. Sandboxing (sandbox.py, opt-in via KYREX_SANDBOX=1)