// cloudClient.ts — Kyrex Cloud backend client for the IDE.
//
// The cloud web experience (sign in with GitHub → submit a task → watch
// live progress → past results) lives on kyrex-cloud's web backend. The
// IDE is a different origin with no shared cookie jar, so every call
// carries the session token in the X-Session-Token header (accepted
// everywhere the browser cookie is — see web/backend/ide_auth.py) and
// the SSE stream uses the ?session= query param, because EventSource
// cannot set headers.
//
// The backend URL and token persist in localStorage: the token grants a
// web session for 7 days, and re-entering it after restarts beats
// re-running OAuth every launch.

export interface CloudMe {
  authenticated: boolean;
  username?: string;
}

export interface CloudResultSummary {
  task: string;
  status: string;
  branch: string;
  started_at: string;
  finished_at: string;
  pr_url: string | null;
  review: { matches_task: boolean; reasoning: string } | null;
  final_response: string;
  errors: string[];
}

export interface CloudSubmitResult {
  status: string;
  task_id: string;
  task: string;
}

/** One flux task event, as delivered by the backend's SSE frames. */
export interface FluxEvent {
  event_id: number | null;
  type: string;
  payload: Record<string, unknown>;
}

const BACKEND_URL_KEY = "kyrex.cloud.backendUrl";
const TOKEN_KEY = "kyrex.cloud.token";

export function getCloudBackendUrl(): string {
  return (localStorage.getItem(BACKEND_URL_KEY) || "").replace(/\/+$/, "");
}

export function setCloudBackendUrl(url: string): void {
  localStorage.setItem(BACKEND_URL_KEY, url.trim().replace(/\/+$/, ""));
}

export function getCloudToken(): string {
  return localStorage.getItem(TOKEN_KEY) || "";
}

export function setCloudToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token.trim());
}

export function clearCloudToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

/** URL the operator opens in a browser to start the IDE OAuth flow.
 *  The callback page there displays the session token to paste back. */
export function buildIdeLoginUrl(baseUrl: string): string {
  return `${baseUrl.replace(/\/+$/, "")}/auth/login?client=ide`;
}

function authHeaders(token: string): Record<string, string> {
  return token ? { "X-Session-Token": token } : {};
}

async function request<T>(
  baseUrl: string,
  token: string,
  path: string,
  opts?: { method?: string; body?: string }
): Promise<T> {
  const resp = await fetch(`${baseUrl}${path}`, {
    method: opts?.method,
    headers: {
      ...authHeaders(token),
      ...(opts?.body ? { "Content-Type": "application/json" } : {}),
    },
    body: opts?.body,
  });
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const body = await resp.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      // non-JSON error body — keep the status-code detail
    }
    throw new Error(detail);
  }
  return (await resp.json()) as T;
}

export function checkCloudAuth(baseUrl: string, token: string): Promise<CloudMe> {
  return request(baseUrl, token, "/api/me");
}

export function submitCloudTask(
  baseUrl: string,
  token: string,
  task: string
): Promise<CloudSubmitResult> {
  return request(baseUrl, token, "/api/task", {
    method: "POST",
    body: JSON.stringify({ task }),
  });
}

export function listCloudResults(
  baseUrl: string,
  token: string
): Promise<{ results: CloudResultSummary[] }> {
  return request(baseUrl, token, "/api/results");
}

export function respondCloudTask(
  baseUrl: string,
  token: string,
  taskId: string,
  text: string
): Promise<{ delivered: boolean }> {
  return request(baseUrl, token, `/api/task/${encodeURIComponent(taskId)}/respond`, {
    method: "POST",
    body: JSON.stringify({ text }),
  });
}

export function cancelCloudTask(
  baseUrl: string,
  token: string,
  taskId: string
): Promise<{ requested: boolean; status: string }> {
  return request(baseUrl, token, `/api/task/${encodeURIComponent(taskId)}/cancel`, {
    method: "POST",
  });
}

/**
 * Streams one task's flux events via Server-Sent Events.
 *
 * The backend frame format (flux.format_sse) is `id:` + `event:` + `data:`,
 * where data is the event payload only — the type arrives on the `event:`
 * line, the cursor on `id:`. Returns a close() function.
 */
export function streamCloudTaskEvents(
  baseUrl: string,
  token: string,
  taskId: string,
  onEvent: (ev: FluxEvent) => void,
  onError?: (err: string) => void
): () => void {
  const url =
    `${baseUrl}/api/task/${encodeURIComponent(taskId)}/events` +
    `?session=${encodeURIComponent(token)}`;
  const es = new EventSource(url);
  const types = [
    "submitted",
    "claimed",
    "status",
    "start",
    "message",
    "edit",
    "progress",
    "approval_requested",
    "approval_resolved",
    "cancelled",
    "result",
    "end",
    "error",
  ];
  for (const type of types) {
    es.addEventListener(type, (e) => {
      let payload: Record<string, unknown> = {};
      try {
        payload = JSON.parse((e as MessageEvent<string>).data);
      } catch {
        onError?.(`malformed SSE payload for event "${type}"`);
        return;
      }
      let eventId: number | null = null;
      if (e.lastEventId) {
        const parsed = Number(e.lastEventId);
        eventId = Number.isNaN(parsed) ? null : parsed;
      }
      onEvent({ event_id: eventId, type, payload });
    });
  }
  es.onerror = () => onError?.("event stream disconnected");
  return () => es.close();
}
