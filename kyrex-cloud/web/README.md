# Kyrex Cloud — Web Trigger

A web-based frontend for Kyrex Cloud, deployable as its own Render service
(separate from the Telegram bot).  Accepts a plain-text task description,
queues it in the shared CloudTaskStore, and follows it live over **Flux** —
the durable, cursor-resumable SSE event stream (`flux.py`, Phase 3).

## Architecture

```
kyrex-cloud/web/
  README.md
  Dockerfile
  backend/
    main.py        — FastAPI app (OAuth, task API, Flux SSE stream,
                     approval + cancel endpoints)
  frontend/
    index.html     — Single-page task UI (EventSource consumer)
```

- **Backend**: FastAPI (Python 3.11+).  Handles GitHub OAuth, queues tasks
  into the shared store (`task_store.CloudTaskStore`), and streams each
  task's events via `GET /api/task/{id}/events` (Server-Sent Events).  The
  stream replays history from a cursor (`?after=` / `Last-Event-ID`), tails
  live, and ends with an in-band `end` event carrying the final status.

- **Frontend**: Single HTML page (no build step).  Task input, live log
  (updated in place, auto-scroll), loading indicator, approval prompts
  (T1 y/n buttons, T2 token reply), a cancel button, and the past results
  list.

- **Task runner**: This app never executes tasks itself.  The worker process
  (`worker.py` → `TaskWorker` → `serve.run_task`) is the single execution
  path, so web tasks pass the same tier/approval/policy/audit gate as
  Telegram tasks.

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
| `KYREX_FLUX_STREAM_MAX_SECONDS` | No | Hard lifetime for one SSE stream (default: `3600`). A walked-away tab cannot pin a poller forever; EventSource reconnects and resumes from its cursor. |
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

# Install dependencies (httpx is needed for the endpoint test layer)
pip install fastapi uvicorn httpx openai anthropic requests

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

## Task execution, approvals, and cancellation

`POST /api/task` queues the task in the shared store and returns its
`task_id`; the frontend immediately opens the Flux stream for that id.
One task runs at a time per session key (an approval-model constraint, not
a throughput one — see `KX_SERVE_DESIGN.md`).

When the worker hits a T1/T2 gate, the task status flips to
`awaiting_approval` and an `approval_requested` event flows down the
stream; the page renders Approve/Deny (T1) or a token reply (T2) and posts
the decision to `POST /api/task/{id}/respond`, which routes it through the
same `serve.handle_approval_reply` protocol Telegram uses.  Every decision
is audited by the worker.

`POST /api/task/{id}/cancel` cancels a queued task immediately and flags a
running task; the worker applies the flag at the next approval gate or at
finalisation.

All three endpoints are session-scoped: a signed-in user can only see and
act on their own tasks (anything else returns 404, which also avoids
leaking task-id existence).