# Kyrex Cloud — Web Trigger

A web-based frontend for Kyrex Cloud, deployable as its own Render service
(separate from the Telegram bot).  Accepts a plain-text task description,
queues it in the shared `CloudTaskStore`, and streams live progress from
the durable Flux event stream (`flux.py`) over Server-Sent Events.

## Architecture

```
kyrex-cloud/web/
  README.md
  Dockerfile
  backend/
    main.py        — FastAPI app (OAuth, task API, Flux SSE stream)
  frontend/
    index.html     — Single-page task UI (EventSource over Flux)
```

- **Backend**: FastAPI (Python 3.11+).  Handles GitHub OAuth, submits tasks
  to the shared store (the worker process is the single execution path,
  gated by `serve.py`), streams task events via
  `GET /api/task/{id}/events` (SSE, cursor-resumable), and routes
  approval replies and cancellation requests back into the store.

- **Frontend**: Single HTML page (no build step).  Task input, live log
  (append history + in-place live line, auto-scroll), approval prompt
  (Approve/Deny), cancel button, past results list.  After a page refresh
  it reattaches to a still-active task — the durable cursor replays
  whatever happened while the tab was closed.

- **Events (Flux)**: `task_events` rows in the store are the source of
  truth.  Real events carry an `id:` (EventSource resumes from
  `Last-Event-ID`); the stream ends with a synthetic `end` event carrying
  the final status.  See `kyrex-cloud/flux.py` and `test_flux.py`.

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

## Task lifecycle

`POST /api/task` queues the task in the shared store and returns its
`task_id` immediately; the worker process executes it.  The frontend
follows `GET /api/task/{id}/events` from that moment: lifecycle events
(`submitted`, `claimed`, `status`), executor output (`start`, `message`,
`edit`, `progress`), approval prompts (`approval_requested` /
`approval_resolved`), and finally `result` + `end`.

While a task runs, the frontend offers:

- **Approve / Deny** on an approval prompt — posts to
  `POST /api/task/{id}/respond` (the same protocol the Telegram bot uses;
  tier-2 token approvals still require the token, so Deny is always
  available and Approve is fail-closed).
- **Cancel** — posts to `POST /api/task/{id}/cancel` (immediate for
  queued tasks, flagged for running tasks).

The Run button is disabled while a task is being followed.