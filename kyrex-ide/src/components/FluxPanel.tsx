import { useEffect, useRef, useState } from "react";
import type { FluxTaskClient, FluxConnectionState, FluxEvent, FluxTaskSummary } from "../lib/fluxClient";
import type { CloudAuthState } from "../lib/cloudAuth";

interface BotCommand {
  cmd: string;
  desc: string;
}

const BOT_COMMANDS: BotCommand[] = [
  { cmd: "/bots", desc: "List all registered bots" },
  { cmd: "/newbot <id> <name> <model>", desc: "Create a bot (e.g. /newbot qa QA-Bot z-ai/glm-5.3-flash)" },
  { cmd: "/startbot <id>", desc: "Set a bot's status to running" },
  { cmd: "/stopbot <id>", desc: "Set a bot's status to stopped" },
  { cmd: "/setbot <id> <field> <value>", desc: "Configure a bot — fields: repo, prompt, model, name" },
];

const BOT_STATUSES: { value: string; className: string }[] = [
  { value: "stopped", className: "bot-status-stopped" },
  { value: "running", className: "bot-status-running" },
  { value: "paused", className: "bot-status-paused" },
];

const AGENT_LABEL: Record<string, string> = {
  default: "Default (web agent)",
};

interface Props {
  authState: CloudAuthState;
  user: string | null;
  cloudUrl: string;
  configured: boolean;
  client: FluxTaskClient | null;
  taskId: string | null;
  status: string;
  connection: FluxConnectionState;
  events: FluxEvent[];
  error: string | null;
  onSubmit: (task: string) => Promise<void>;
  onCancel: () => Promise<void>;
  onDismiss: () => void;
  onClearError: () => void;
  onReopen: (taskId: string) => void;
  onSignIn: () => void;
}

const TERMINAL_STATUSES = new Set(["done", "failed", "cancelled", "complete"]);

function isTerminal(status: string): boolean {
  return TERMINAL_STATUSES.has(status.toLowerCase());
}

function formatPayload(event: FluxEvent): string {
  const payload = event.payload ?? {};
  switch (event.type) {
    case "status": {
      const s = typeof payload.status === "string" ? payload.status : "";
      return `status → ${s}`;
    }
    case "message":
    case "start":
    case "edit": {
      const t = typeof payload.text === "string" ? payload.text : "";
      return t || JSON.stringify(payload);
    }
    case "progress": {
      const entries = Object.entries(payload);
      return entries.length
        ? entries.map(([k, v]) => `${k}: ${String(v)}`).join(" · ")
        : "(progress)";
    }
    case "approval_requested": {
      const summary = typeof payload.summary === "string" ? payload.summary : "";
      const tier = typeof payload.tier === "number" ? payload.tier : "?";
      const id = typeof payload.approval_id === "string" ? payload.approval_id : "";
      return `T${tier} approval requested${summary ? ` — ${summary}` : ""}${id ? ` (${id})` : ""}`;
    }
    case "approval_resolved": {
      const d = typeof payload.decision === "string" ? payload.decision : "";
      return `approval resolved — ${d}`;
    }
    case "cancelled": {
      const r = typeof payload.reason === "string" ? payload.reason : "";
      return r || "task cancelled";
    }
    case "error": {
      const e = typeof payload.error === "string" ? payload.error : "";
      return e || JSON.stringify(payload);
    }
    case "result": {
      const fr = typeof payload.final_response === "string" ? payload.final_response : "";
      const st = typeof payload.status === "string" ? payload.status : "";
      if (fr) return `final response (${st || "done"}): ${fr.slice(0, 200)}`;
      if (st) return `result — ${st}`;
      return JSON.stringify(payload).slice(0, 200);
    }
    case "end": {
      const s = typeof payload.status === "string" ? payload.status : "complete";
      const r = typeof payload.reason === "string" ? ` (${payload.reason})` : "";
      return `task finished — ${s}${r}`;
    }
    default:
      return JSON.stringify(payload).slice(0, 200);
  }
}

const CONNECTION_LABEL: Record<FluxConnectionState, string> = {
  idle: "idle",
  connecting: "connecting",
  open: "streaming",
  reconnecting: "reconnecting",
  closed: "closed",
  error: "error",
};

export default function FluxPanel({
  authState,
  user,
  cloudUrl,
  configured,
  client,
  taskId,
  status,
  connection,
  events,
  error,
  onSubmit,
  onCancel,
  onDismiss,
  onClearError,
  onReopen,
  onSignIn,
}: Props) {
  const [taskText, setTaskText] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [cancelRequested, setCancelRequested] = useState(false);
  const [history, setHistory] = useState<FluxTaskSummary[] | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [agent, setAgent] = useState<string>("default");
  const [botControlsOpen, setBotControlsOpen] = useState(false);
  const streamRef = useRef<HTMLDivElement | null>(null);
  const followStreamRef = useRef(true);

  const authenticated = authState === "authenticated";
  const hasTask = taskId !== null;
  const terminal = hasTask && isTerminal(status);
  const busy = hasTask && !terminal && connection !== "idle";

  useEffect(() => {
    const el = streamRef.current;
    if (el && followStreamRef.current) el.scrollTop = el.scrollHeight;
  }, [events]);

  useEffect(() => {
    setCancelRequested(false);
  }, [taskId]);

  useEffect(() => {
    let cancelled = false;
    if (!authenticated || !client) return;
    setHistoryLoading(true);
    client.listResults()
      .then((results) => { if (!cancelled) { setHistory(results); setHistoryError(null); } })
      .catch((e) => { if (!cancelled) setHistoryError(e instanceof Error ? e.message : String(e)); })
      .finally(() => { if (!cancelled) setHistoryLoading(false); });
    return () => { cancelled = true; };
  }, [authenticated, client]);

  async function handleSubmit() {
    const text = taskText.trim();
    if (!text || submitting || !authenticated || !client) return;
    setSubmitting(true);
    try {
      await onSubmit(text);
      setTaskText("");
    } catch {
      // Error surfaces through the error prop.
    } finally {
      setSubmitting(false);
    }
  }

  async function handleCancel() {
    if (!client || !taskId || cancelRequested) return;
    setCancelRequested(true);
    try {
      await onCancel();
    } catch {
      // Error surfaces through the error prop.
    }
  }

  function handleStreamScroll(event: React.UIEvent<HTMLDivElement>) {
    const el = event.currentTarget;
    followStreamRef.current = el.scrollHeight - el.scrollTop - el.clientHeight <= 40;
  }

  let cloudHost = cloudUrl;
  try {
    cloudHost = new URL(cloudUrl).host || cloudUrl;
  } catch {
    // keep raw value
  }

  if (!configured) {
    return (
      <div className="cloud-workspace">
        <div className="cloud-workspace-header"><span className="cloud-workspace-title">FLUX</span><span className="cloud-workspace-sub">Cloud task streaming</span></div>
        <div className="cloud-state-card">
          <div className="activity-empty-icon">≋</div>
          <b>Kyrex Cloud is not configured</b>
          <span>Set the <code>VITE_KYREX_CLOUD_URL</code> environment variable at build time, then rebuild the IDE, to enable Flux cloud tasks.</span>
        </div>
      </div>
    );
  }

  if (authState === "checking") {
    return (
      <div className="cloud-workspace">
        <div className="cloud-workspace-header"><span className="cloud-workspace-title">FLUX</span><span className="cloud-workspace-sub">Cloud task streaming</span></div>
        <div className="cloud-state-card"><span>Checking Cloud authentication…</span></div>
      </div>
    );
  }

  if (!authenticated) {
    return (
      <div className="cloud-workspace">
        <div className="cloud-workspace-header"><span className="cloud-workspace-title">FLUX</span><span className="cloud-workspace-sub">Cloud task streaming</span></div>
        <div className="cloud-state-card">
          <div className="activity-empty-icon">≋</div>
          <b>{authState === "expired" ? "Cloud session expired" : authState === "error" ? "Cloud authentication unavailable" : "Sign in to Kyrex Cloud"}</b>
          <span>Flux tasks are submitted to Kyrex Cloud and stream live through a secure session. Sign in to continue.</span>
          {authState !== "error" && <button className="cloud-primary-btn" onClick={onSignIn}>Sign in</button>}
        </div>
      </div>
    );
  }

  return (
    <div className="cloud-workspace">
      <div className="cloud-workspace-header">
        <span className="cloud-workspace-title">FLUX</span>
        <span className="cloud-workspace-sub">Cloud task streaming</span>
        <span className={`flux-badge flux-connection-${connection}`}>{CONNECTION_LABEL[connection]}</span>
        {hasTask && <span className={`flux-badge flux-status-${status.replace(/[- ]/g, "_") || "default"}`}>{status || "…"}</span>}
      </div>

      <div className="flux-context">
        <span>Session: <b>{user ?? "—"}</b></span>
        <span>Cloud: <b>{cloudHost}</b></span>
        <span>Agent: <b>{AGENT_LABEL[agent] ?? agent}</b></span>
        {hasTask ? <span>Task: <b className="flux-task-id">{taskId}</b></span> : <span>No task selected</span>}
      </div>

      <div className="flux-agents">
        <div className="flux-agents-header">
          <span className="cloud-workspace-sub">AGENTS</span>
          <select
            className="flux-agent-select"
            value={agent}
            onChange={(e) => setAgent(e.target.value)}
            aria-label="Select agent"
          >
            <option value="default">Default — unbound web agent</option>
          </select>
          <button className="cloud-secondary-btn" onClick={() => setBotControlsOpen((v) => !v)}>
            {botControlsOpen ? "Hide bot management" : "Bot management"}
          </button>
        </div>
        {botControlsOpen && (
          <div className="cloud-state-columns">
            <div className="cloud-state-card">
              <div className="activity-empty-icon">◆</div>
              <b>No HTTP bot-management API exposed yet</b>
              <span>
                Kyrex Cloud does not currently expose an HTTP endpoint for its bot registry to the IDE.
                The registry (<code>bots.json</code> on the Cloud host) is the single source of truth and is
                managed server-side through the Kyrex Cloud Telegram bot. No bot data is fabricated or mirrored here.
              </span>
            </div>

            <div className="cloud-state-card">
              <div className="cloud-state-card-title">BOT LIFECYCLE — TELEGRAM COMMANDS</div>
              {BOT_COMMANDS.map((c) => (
                <div key={c.cmd} className="bot-command">
                  <code>{c.cmd}</code>
                  <span>{c.desc}</span>
                </div>
              ))}
            </div>

            <div className="cloud-state-card">
              <div className="cloud-state-card-title">BOT STATUS VALUES</div>
              {BOT_STATUSES.map((s) => (
                <div key={s.value} className="bot-status-row"><i className={`bot-status-dot ${s.className}`} />{s.value}</div>
              ))}
              <div className="cloud-workspace-sub" style={{ marginTop: 6 }}>
                Address a bot in Telegram with <code>@&lt;id&gt;: &lt;task&gt;</code> to bind it. Web tasks are submitted unbound.
              </div>
            </div>

            <div className="cloud-state-card">
              <div className="cloud-state-card-title">NEXT STEP</div>
              <span>
                To surface live bots as selectable agents here, Kyrex Cloud needs to expose its existing bot registry over HTTP
                (e.g. list / create / start / stop / configure). The registry functions in <code>bots.py</code> and the
                existing task/SSE infrastructure can then back a dedicated Bots API. Until then this section is informational only,
                and Flux submits tasks through the default web agent.
              </span>
            </div>
          </div>
        )}
      </div>

      {error && (
        <div className="flux-error-banner">
          <span>Cloud error: {error}</span>
          <button onClick={onClearError} title="Clear error">✕</button>
        </div>
      )}

      {connection === "reconnecting" && (
        <div className="flux-reconnecting-banner">Connection lost — reconnecting with cursor resume…</div>
      )}

      {terminal && (
        <div className="flux-finished-banner">
          Task finished — {status}. <button onClick={onDismiss}>Dismiss</button>
        </div>
      )}

      <div className="flux-composer">
        <textarea
          value={taskText}
          onChange={(e) => setTaskText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSubmit(); } }}
          placeholder="Describe the task for Kyrex Cloud…"
          rows={3}
        />
        <div className="flux-composer-actions">
          <button className="cloud-primary-btn" onClick={handleSubmit} disabled={!taskText.trim() || submitting || !client || busy}>
            {submitting ? "Submitting…" : busy ? "Task in progress" : "Submit task"}
          </button>
          {busy && (
            <button className="cloud-secondary-btn" onClick={handleCancel} disabled={cancelRequested}>
              {cancelRequested ? "Cancel requested…" : "Cancel"}
            </button>
          )}
          {terminal && <button className="cloud-secondary-btn" onClick={onDismiss}>Start fresh</button>}
          {error && <button className="cloud-secondary-btn" onClick={onClearError}>Clear error</button>}
          <span className="flux-note">Shift + Enter for newline</span>
        </div>
      </div>

      <div
        ref={streamRef}
        className="flux-stream"
        onScroll={handleStreamScroll}
      >
        {events.length === 0 && (
          <div className="flux-empty">
            {hasTask && connection === "connecting" ? "Connecting to task stream…" : "No events yet — submit a task, or reopen one from Recent tasks."}
          </div>
        )}
        {events.map((event, i) => (
          <div key={`${event.event_id ?? "synthetic"}-${i}`} className={`flux-event flux-event-${event.type}`}>
            <span className="flux-event-type">{event.type}</span>
            {event.event_id !== null && <small className="flux-event-id">#{event.event_id}</small>}
            <span className="flux-event-payload">{formatPayload(event)}</span>
          </div>
        ))}
      </div>

      <div className="flux-history">
        <div className="flux-history-header">
          <span>RECENT TASKS</span>
          <button onClick={() => {
            setHistoryLoading(true);
            client?.listResults()
              .then((results) => { setHistory(results); setHistoryError(null); })
              .catch((e) => setHistoryError(e instanceof Error ? e.message : String(e)))
              .finally(() => setHistoryLoading(false));
          }}>↻</button>
        </div>
        {historyLoading && <div className="flux-history-empty">Loading…</div>}
        {!historyLoading && historyError && <div className="flux-history-error">Could not load recent tasks: {historyError}</div>}
        {!historyLoading && !historyError && history !== null && history.length === 0 && (
          <div className="flux-history-empty">No tasks yet in this Cloud session.</div>
        )}
        {!historyLoading && !historyError && (history ?? []).map((item) => (
          <button key={item.task_id} className="flux-history-row" onClick={() => onReopen(item.task_id)}>
            <span className={`flux-history-status flux-status-chip-${item.status}`}>{item.status}</span>
            <span className="flux-history-task">{item.task || "(no text)"}</span>
            <span className="flux-history-meta">
              {item.task_id}{item.branch ? ` · ${item.branch}` : ""}
              {typeof item.final_response === "string" && item.final_response.length > 0 ? ` · ${item.final_response.slice(0, 120)}` : ""}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}