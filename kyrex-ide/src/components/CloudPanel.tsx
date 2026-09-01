import { useCallback, useEffect, useRef, useState } from "react";
import { openUrl } from "@tauri-apps/plugin-opener";
import {
  buildIdeLoginUrl,
  cancelCloudTask,
  checkCloudAuth,
  clearCloudToken,
  getCloudBackendUrl,
  getCloudToken,
  listCloudResults,
  respondCloudTask,
  setCloudBackendUrl,
  setCloudToken,
  streamCloudTaskEvents,
  submitCloudTask,
  type CloudResultSummary,
  type FluxEvent,
} from "../lib/cloudClient";

// CloudPanel — the Kyrex Cloud web experience, moved into the IDE.
//
// Sign in with GitHub (system browser, token pasted back), submit a task
// to the shared store, follow its flux event stream live (progress,
// approvals, result), approve/deny/cancel from here, and browse past
// results. Same backend endpoints as the browser UI, authenticated with
// X-Session-Token instead of the cookie.

interface LogEntry {
  kind: "info" | "line" | "approval" | "result" | "error";
  text: string;
  prUrl?: string;
}

interface Props {
  onClose: () => void;
}

export default function CloudPanel({ onClose }: Props) {
  const [backendUrl, setUrl] = useState<string>(getCloudBackendUrl());
  const [urlDraft, setUrlDraft] = useState<string>(getCloudBackendUrl());
  const [editingUrl, setEditingUrl] = useState<boolean>(!getCloudBackendUrl());
  const [tokenDraft, setTokenDraft] = useState<string>("");
  const [username, setUsername] = useState<string | null>(null);
  const [authChecking, setAuthChecking] = useState<boolean>(false);
  const [task, setTask] = useState<string>("");
  const [running, setRunning] = useState<boolean>(false);
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);
  const [log, setLog] = useState<LogEntry[]>([]);
  const [replyDraft, setReplyDraft] = useState<string>("");
  const [results, setResults] = useState<CloudResultSummary[]>([]);
  const [resultsLoaded, setResultsLoaded] = useState<boolean>(false);
  const closeStreamRef = useRef<(() => void) | null>(null);
  const logBottomRef = useRef<HTMLDivElement | null>(null);

  const addLog = useCallback((entry: LogEntry) => {
    setLog((prev) => [...prev, entry]);
  }, []);

  const loadResults = useCallback(
    async (url: string, token: string) => {
      try {
        const data = await listCloudResults(url, token);
        setResults(data.results || []);
        setResultsLoaded(true);
      } catch (e) {
        addLog({ kind: "error", text: `Failed to load results: ${e}` });
      }
    },
    [addLog]
  );

  // Validate the persisted token on mount; refresh past results too.
  useEffect(() => {
    const url = getCloudBackendUrl();
    const token = getCloudToken();
    if (!url || !token) return;
    setAuthChecking(true);
    checkCloudAuth(url, token)
      .then((me) => {
        if (me.authenticated && me.username) {
          setUsername(me.username);
          loadResults(url, token);
        } else {
          clearCloudToken();
        }
      })
      .catch(() => clearCloudToken())
      .finally(() => setAuthChecking(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    logBottomRef.current?.scrollIntoView({ block: "end" });
  }, [log]);

  useEffect(() => () => closeStreamRef.current?.(), []);

  function handleSaveUrl() {
    const url = urlDraft.trim().replace(/\/+$/, "");
    if (!url) return;
    setCloudBackendUrl(url);
    setUrl(url);
    setEditingUrl(false);
    setResultsLoaded(false);
  }

  async function handleSignIn() {
    if (!backendUrl) {
      setEditingUrl(true);
      return;
    }
    try {
      await openUrl(buildIdeLoginUrl(backendUrl));
      addLog({
        kind: "info",
        text: "GitHub sign-in opened in your browser. Paste the session token below to connect.",
      });
    } catch (e) {
      addLog({ kind: "error", text: `Could not open the browser: ${e}` });
    }
  }

  async function handleConnect() {
    const token = tokenDraft.trim();
    if (!backendUrl || !token) return;
    setAuthChecking(true);
    try {
      const me = await checkCloudAuth(backendUrl, token);
      if (me.authenticated && me.username) {
        setCloudToken(token);
        setTokenDraft("");
        setUsername(me.username);
        loadResults(backendUrl, token);
      } else {
        addLog({ kind: "error", text: "That token was not accepted." });
      }
    } catch (e) {
      addLog({ kind: "error", text: `Sign-in failed: ${e}` });
    } finally {
      setAuthChecking(false);
    }
  }

  function handleSignOut() {
    closeStreamRef.current?.();
    closeStreamRef.current = null;
    clearCloudToken();
    setUsername(null);
    setResults([]);
    setResultsLoaded(false);
    setRunning(false);
    setActiveTaskId(null);
  }

  function renderEvent(ev: FluxEvent): void {
    const p = ev.payload as Record<string, unknown>;
    const str = (v: unknown, fallback = ""): string =>
      typeof v === "string" ? v : v === undefined || v === null ? fallback : String(v);
    const text = str(p.text);
    switch (ev.type) {
      case "start":
      case "message":
      case "edit":
        if (text) addLog({ kind: "line", text });
        break;
      case "progress": {
        const pairs = Object.entries(p)
          .map(([k, v]) => `${k}: ${str(v, "")}`)
          .join("  ");
        if (pairs) addLog({ kind: "line", text: `→ ${pairs}` });
        break;
      }
      case "approval_requested":
        addLog({
          kind: "approval",
          text: `Approval requested (tier ${str(p.tier, "?")}): ${str(p.summary)}`,
        });
        break;
      case "approval_resolved":
        addLog({ kind: "info", text: `Approval resolved: ${str(p.decision)}` });
        break;
      case "result": {
        const status = str(p.status, "unknown");
        const pr = str(p.pr_url) || undefined;
        const response = str(p.final_response);
        addLog({
          kind: "result",
          text: `── Result: ${status}${response ? ` ──\n${response}` : " ──"}`,
          prUrl: pr,
        });
        break;
      }
      case "end":
        addLog({ kind: "info", text: `Task ${str(p.status, "finished")}.` });
        break;
      case "error":
        addLog({ kind: "error", text: str(p.error, "stream error") });
        break;
      default:
        break;
    }
  }

  async function handleRun() {
    const text = task.trim();
    if (!text || running || !backendUrl || !username) return;
    setRunning(true);
    setLog([]);
    addLog({ kind: "info", text: "Submitting task..." });
    try {
      const submitted = await submitCloudTask(backendUrl, getCloudToken(), text);
      setActiveTaskId(submitted.task_id);
      addLog({ kind: "info", text: `Task ${submitted.task_id} queued.` });
      closeStreamRef.current = streamCloudTaskEvents(
        backendUrl,
        getCloudToken(),
        submitted.task_id,
        (ev) => {
          renderEvent(ev);
          if (ev.type === "end") {
            setRunning(false);
            loadResults(backendUrl, getCloudToken());
          }
        },
        (err) => addLog({ kind: "error", text: err })
      );
      setTask("");
    } catch (e) {
      addLog({ kind: "error", text: `Submission failed: ${e}` });
      setRunning(false);
    }
  }

  async function handleReply(text: string) {
    if (!activeTaskId || !backendUrl) return;
    try {
      await respondCloudTask(backendUrl, getCloudToken(), activeTaskId, text);
      addLog({ kind: "info", text: `Reply sent: ${text}` });
      setReplyDraft("");
    } catch (e) {
      addLog({ kind: "error", text: `Reply failed: ${e}` });
    }
  }

  async function handleCancel() {
    if (!activeTaskId || !backendUrl) return;
    try {
      await cancelCloudTask(backendUrl, getCloudToken(), activeTaskId);
      addLog({ kind: "info", text: "Cancellation requested." });
    } catch (e) {
      addLog({ kind: "error", text: `Cancel failed: ${e}` });
    }
  }

  const pendingApproval = [...log].reverse().find((l) => l.kind === "approval");

  return (
    <div className="cloud-panel">
      <div className="cloud-panel-header">
        <span>Kyrex Cloud</span>
        <div className="cloud-header-actions">
          <button
            className="cloud-url-btn"
            onClick={() => setEditingUrl((v) => !v)}
            title="Cloud backend URL"
          >
            ⛁
          </button>
          <button onClick={onClose}>×</button>
        </div>
      </div>

      <div className="cloud-panel-body">
        {editingUrl && (
          <div className="cloud-card">
            <div className="cloud-card-title">Cloud backend URL</div>
            <div className="cloud-input-row">
              <input
                type="text"
                value={urlDraft}
                placeholder="https://kyrex-cloud.onrender.com"
                onChange={(e) => setUrlDraft(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleSaveUrl()}
              />
              <button onClick={handleSaveUrl}>Save</button>
            </div>
          </div>
        )}

        {!username ? (
          <div className="cloud-card">
            {authChecking ? (
              <div className="cloud-dim">Checking sign-in...</div>
            ) : (
              <>
                <button className="cloud-btn-primary" onClick={handleSignIn}>
                  Sign in with GitHub
                </button>
                <div className="cloud-input-row cloud-token-row">
                  <input
                    type="password"
                    value={tokenDraft}
                    placeholder="Paste session token"
                    onChange={(e) => setTokenDraft(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && handleConnect()}
                  />
                  <button
                    onClick={handleConnect}
                    disabled={!tokenDraft.trim() || !backendUrl}
                  >
                    Connect
                  </button>
                </div>
              </>
            )}
          </div>
        ) : (
          <>
            <div className="cloud-auth-bar">
              <span>
                Signed in as <strong>{username}</strong>
              </span>
              <button onClick={handleSignOut}>Sign out</button>
            </div>

            <div className="cloud-card">
              <div className="cloud-card-title">Run a task</div>
              <div className="cloud-input-row">
                <textarea
                  value={task}
                  placeholder="Describe what the cloud agent should do..."
                  onChange={(e) => setTask(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      handleRun();
                    }
                  }}
                />
                <button
                  className="cloud-btn-primary"
                  onClick={handleRun}
                  disabled={running || !task.trim()}
                >
                  Run
                </button>
              </div>
              {running && (
                <div className="cloud-running-row">
                  <span className="cloud-spinner" />
                  <span>Task running...</span>
                  <button className="cloud-cancel-btn" onClick={handleCancel}>
                    Cancel
                  </button>
                </div>
              )}
            </div>

            {log.length > 0 && (
              <div className="cloud-card">
                <div className="cloud-card-title">Progress</div>
                <div className="cloud-log">
                  {log.map((entry, i) => (
                    <div key={i} className={`cloud-log-entry cloud-${entry.kind}`}>
                      {entry.kind === "result" && entry.prUrl ? (
                        <>
                          {entry.text}
                          {"\n"}
                          <a href={entry.prUrl} target="_blank" rel="noreferrer">
                            {entry.prUrl}
                          </a>
                        </>
                      ) : (
                        entry.text
                      )}
                    </div>
                  ))}
                  {pendingApproval && running && (
                    <div className="cloud-approval-actions">
                      <button onClick={() => handleReply("y")}>Approve</button>
                      <button onClick={() => handleReply("n")}>Deny</button>
                      <input
                        type="text"
                        value={replyDraft}
                        placeholder="or type a reply (e.g. token)"
                        onChange={(e) => setReplyDraft(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter" && replyDraft.trim()) {
                            handleReply(replyDraft.trim());
                          }
                        }}
                      />
                    </div>
                  )}
                  <div ref={logBottomRef} />
                </div>
              </div>
            )}

            <div className="cloud-card">
              <div className="cloud-card-title">Past results</div>
              {!resultsLoaded ? (
                <div className="cloud-dim">Loading...</div>
              ) : results.length === 0 ? (
                <div className="cloud-dim">No results yet.</div>
              ) : (
                results.map((r, i) => (
                  <div key={i} className="cloud-result">
                    <div className="cloud-result-task">{r.task}</div>
                    <div className="cloud-result-meta">
                      <span className={`cloud-status cloud-status-${r.status}`}>
                        {r.status.replace(/_/g, " ")}
                      </span>
                      {r.pr_url && (
                        <a href={r.pr_url} target="_blank" rel="noreferrer">
                          PR ↗
                        </a>
                      )}
                      {r.finished_at && (
                        <span>{new Date(r.finished_at).toLocaleString()}</span>
                      )}
                    </div>
                    {r.final_response && (
                      <div className="cloud-result-response">
                        {r.final_response.substring(0, 200)}
                      </div>
                    )}
                  </div>
                ))
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
