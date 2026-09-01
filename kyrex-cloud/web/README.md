# Kyrex Cloud — Web Trigger

A web-based frontend for Kyrex Cloud, deployable as its own Render service
(separate from the Telegram bot).  Accepts a plain-text task description,
submits it to the shared `CloudTaskStore` (the worker process is the single
execution path), and streams live progress over **Flux** — the durable,
cursor-based SSE task event stream (`flux.py`).

## Architecture

```
kyrex-cloud/web/
  README.md
  Dockerfile
  backend/
    main.py        — FastAPI app (OAuth, task API, Flux SSE stream)
  frontend/
    index.html     — Single-page task UI
```

- **Backend**: FastAPI (Python 3.11+).  Handles GitHub OAuth, submits tasks
  to the shared `CloudTaskStore` (the worker is the single execution path,
  so every task goes through the same tier/approval/policy/audit gate in
  `serve.py`; web sessions never resolve a Bot binding), and streams live
  task events via the Flux SSE endpoint (`GET /api/task/{id}/events` —
  replays history from the cursor, tails live, ends with an explicit `end`
  event; resumable via `Last-Event-ID`).  Past results are scoped to the
  authenticated user.

- **Frontend**: Single HTML page (no build step).  Task input, live Flux
  event log (updated in place, auto-scroll), inline approval replies
  (y/n buttons for T1, typed token for T2), task cancellation, loading
  indicator, past results list.  On reload it reattaches to an active
  task's stream and replays it from the store.

- **Task runner**: `kyrex-cloud/worker.py` (TaskWorker) claims submitted
  tasks and runs them through the existing `serve.run_task` path with thin
  persistence callbacks — the same executor contract as the Telegram bot.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `GITHUB_CLIENT_ID` | **Yes** | GitHub OAuth App client ID |
| `GITHUB_CLIENT_SECRET` | **Yes** | GitHub OAuth App client secret |
| `WEB_ALLOWED_GITHUB_USERNAME` | **Yes** | Only this GitHub username may sign in |
| `GITHUB_TOKEN` | No | GitHub personal access token (for opening PRs) |
| `KYREX_TARGET_REPO_URL` | No | Target repo URL (default: `https://github.com/kp84-hub/kyrex.git`) |
| `KYREX_TARGET_BASE` | No | Base branch (default: `main`) |
| `WEB_SESSION_SECRET` | No | Session signing secret (auto-generated if empty) |
| `PORT` | No | HTTP port (default: `8000`) |

### Provider env vars (passed through to `git_workflow.py`)

| Variable | Required | Description |
|---|---|---|
| `KYREX_PROVIDER` | **Yes** | `"openai"` or `"anthropic"` |
| `KYREX_API_KEY` | **Yes** | API key for the chosen provider |
| `KYREX_MODEL` | **Yes** | Model name (e.g. `"gpt-4o"`, `"claude-sonnet-4-20250514"`) |
| `OPENAI_BASE_URL` | No | Custom OpenAI-compatible base URL |

## GitHub OAuth setup

1. Go to **Settings → Developer settings → OAuth Apps → New OAuth App**
2. Set **Homepage URL** to `https://your-render-service.onrender.com`
3. Set **Authorization callback URL** to `https://your-render-service.onrender.com/auth/callback`
4. Copy the Client ID and Client Secret to your env vars
5. Set `WEB_ALLOWED_GITHUB_USERNAME` to your GitHub username

## Deploy to Render

1. Create a new **Web Service** on Render
2. Connect your GitHub repository
3. Set:
   - **Root Directory**: repo root (the `kyrex/` monorepo root, not `kyrex-cloud/web/`)
   - **Dockerfile Path**: `kyrex-cloud/web/Dockerfile`
   - **Health Check Path**: `/api/me`
4. Add all environment variables listed above
5. Deploy

This service is independent from the Telegram bot — they use different
Dockerfiles and different ports, so one never forces a re-deploy of the other.

## Local development

```bash
# From the repo root
cd kyrex-cloud/web

# Install dependencies
pip install fastapi uvicorn openai anthropic requests

# Set env vars (copy from .env or export)
export GITHUB_CLIENT_ID=...
export GITHUB_CLIENT_SECRET=...
export WEB_ALLOWED_GITHUB_USERNAME=...

# Run
python3 backend/main.py
```

Then open `http://localhost:8000` in a browser.

For the OAuth callback to work locally, either:
- Use a tool like `ngrok` to expose `localhost:8000`, and update the GitHub
  OAuth app's callback URL to the ngrok URL, or
- Set the callback URL to `http://localhost:8000/auth/callback` in the
  GitHub OAuth app settings (works for local testing).

## Single-task busylock

Like the Telegram bot, the web trigger runs one task at a time.  If a task
is already running, `POST /api/task` returns HTTP 429.  The frontend
disables the Run button while a task is in progress.