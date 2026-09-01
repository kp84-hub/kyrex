import { useState, useRef, useEffect, useCallback } from "react";
import "./App.css";
import { invoke } from "@tauri-apps/api/core";
import { open, confirm } from "@tauri-apps/plugin-dialog";
import { startEngine, sendToEngine, type EngineMessage } from "./lib/engineClient";
import EditApproval, { type ProposedEdit } from "./components/EditApproval";
import FileTree from "./components/FileTree";
import CodeEditor from "./components/CodeEditor";
import TerminalPanel from "./components/TerminalPanel";
import SettingsPanel from "./components/SettingsPanel";
import RaceView from "./components/RaceView";
import CloudPanel from "./components/CloudPanel";
import SetupWizard from "./components/SetupWizard";
import { homeDir, join } from "@tauri-apps/api/path";
import { getVersion } from "@tauri-apps/api/app";

interface TerminalEntry {
  command: string;
  output: string;
  returncode: number | null;
  timestamp: number;
}

interface ChatLine {
  role: "user" | "agent" | "system";
  content: string;
}

export default function App() {
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [openFile, setOpenFile] = useState<{ path: string; content: string } | null>(null);
  const [editorDirty, setEditorDirty] = useState(false);
  const [lines, setLines] = useState<ChatLine[]>([
    { role: "system", content: "Starting engine..." },
  ]);
  const [input, setInput] = useState("");
  const [engineReady, setEngineReady] = useState(false);
  const [pendingEdit, setPendingEdit] = useState<ProposedEdit | null>(null);
  const [workspacePath, setWorkspacePath] = useState<string | null>(null);
  const [workspaceLoading, setWorkspaceLoading] = useState(true);
  const streamingRef = useRef<string>("");
  const isStreamingRef = useRef<boolean>(false);
  const [terminalOpen, setTerminalOpen] = useState(false);
  const [terminalLog, setTerminalLog] = useState<TerminalEntry[]>([]);
  const [needsSetup, setNeedsSetup] = useState<boolean | null>(null);
  const [configPath, setConfigPath] = useState<string | null>(null);
  const [appVersion, setAppVersion] = useState<string>("");
  const [currentSession, setCurrentSession] = useState<string>("main");
  const [sessions, setSessions] = useState<string[]>([]);
  const [sessionDropdownOpen, setSessionDropdownOpen] = useState(false);
  const pendingSessionSwitch = useRef<string | null>(null);
  const sessionsBeforeNew = useRef<string[] | null>(null);
  const [autoApprove, setAutoApprove] = useState(false);
  const [autoApproveDelay, setAutoApproveDelay] = useState(5);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [raceViewOpen, setRaceViewOpen] = useState(false);
  const [cloudViewOpen, setCloudViewOpen] = useState(false);

  useEffect(() => {
    getVersion().then(setAppVersion).catch(() => setAppVersion(""));
  }, []);

  const workspacePathRef = useRef<string | null>(null);
  useEffect(() => { workspacePathRef.current = workspacePath; }, [workspacePath]);

  // ── Provider config check (must happen before engine boot) ────────────
  useEffect(() => {
    let cancelled = false;

    async function checkConfig() {
      try {
        const home = await homeDir();
        const path = await join(home, ".px", "config.json");
        if (!cancelled) setConfigPath(path);
        const contents = await invoke<string>("read_file_contents", { path });
        if (!cancelled) {
          setNeedsSetup(contents.trim().length === 0);
        }
      } catch (e) {
        console.error("failed to check provider config", e);
        if (!cancelled) setNeedsSetup(true);
      }
    }

    checkConfig();
    return () => { cancelled = true; };
  }, []);

  // ── Workspace resolution ─────────────────────────────────────────────
  useEffect(() => {
    let cancelled = false;

    async function resolveWorkspace() {
      setWorkspaceLoading(true);
      try {
        const saved = await invoke<string | null>("load_workspace_config");
        if (!cancelled && saved) {
          setWorkspacePath(saved);
        }
      } catch (e) {
        console.error("failed to load workspace config", e);
      } finally {
        if (!cancelled) setWorkspaceLoading(false);
      }
    }

    resolveWorkspace();
    return () => { cancelled = true; };
  }, []);

  // Boot engine once workspace is resolved
  const bootEngine = useCallback(async (path: string) => {
    try {
      await startEngine(
        path,
        (msg: EngineMessage) => handleMessage(msg),
        (err) => {
          setLines((prev) => [...prev, { role: "system", content: `[parse error] ${err}` }]);
        },
        (errLine) => {
          setLines((prev) => [...prev, { role: "system", content: `[stderr] ${errLine}` }]);
        },
        () => {
          setLines((prev) => [...prev, { role: "system", content: "[engine closed]" }]);
        }
      );
      setEngineReady(true);
      setLines((prev) => [...prev, { role: "system", content: "Engine ready." }]);
    } catch (e) {
      setLines((prev) => [...prev, { role: "system", content: `[failed to start engine] ${e}` }]);
    }
  }, []);

  // Boot engine when workspace path is set
  const prevWorkspaceRef = useRef<string | null>(null);
  useEffect(() => {
    if (workspacePath && needsSetup === false && workspacePath !== prevWorkspaceRef.current) {
      prevWorkspaceRef.current = workspacePath;
      bootEngine(workspacePath);
    }
  }, [workspacePath, needsSetup, bootEngine]);

  async function handleSelectWorkspace() {
    try {
      const selected = await open({ directory: true, multiple: false, title: "Select Project Folder" });
      if (selected) {
        const path = typeof selected === "string" ? selected : selected;
        setWorkspacePath(path);
        await invoke("save_workspace_config", { path });
      }
    } catch (e) {
      setLines((prev) => [...prev, { role: "system", content: `[workspace picker error] ${e}` }]);
    }
  }

  // ── Message handling ─────────────────────────────────────────────────
  async function handleMessage(msg: EngineMessage) {
    switch (msg.type) {
      case "token": {
        streamingRef.current += msg.content ?? "";
        const text = streamingRef.current;
        setLines((prev) => {
          if (isStreamingRef.current) {
            const copy = [...prev];
            copy[copy.length - 1] = { role: "agent", content: text };
            return copy;
          } else {
            isStreamingRef.current = true;
            return [...prev, { role: "agent", content: text }];
          }
        });
        break;
      }
      case "chat_done": {
        streamingRef.current = "";
        isStreamingRef.current = false;

        const pending = pendingSessionSwitch.current;
        if (pending) {
          const wp = workspacePathRef.current;
          if (wp) {
            try {
              const newSessions = await invoke<string[]>("list_sessions", { workspacePath: wp });
              setSessions(newSessions);

              let resolved: string;
              if (sessionsBeforeNew.current) {
                const before = new Set(sessionsBeforeNew.current);
                const created = newSessions.filter((s) => !before.has(s));
                resolved = created.length > 0 ? created[0] : pending;
                sessionsBeforeNew.current = null;
              } else {
                resolved = pending;
              }
              setCurrentSession(resolved);
              invoke("save_session_config", { name: resolved });
            } catch {
              setCurrentSession(pending);
              invoke("save_session_config", { name: pending });
            }
          }
          pendingSessionSwitch.current = null;
        }
        break;
      }
      case "propose_edit": {
        const editId = msg.editId as string;
        const filePath = msg.filePath as string;
        const content = (msg.content as string) ?? "";
        setPendingEdit({ editId, filePath, content });
        break;
      }
      case "system": {
        const content = msg.content ?? "";

        // Detect session-switch confirmations from /new and /checkout
        const newSessionMatch = content.match(/\[\*\] Forked to new branch: (\S+)/);
        const checkoutMatch = content.match(/\[\*\] Switched to branch: (\S+)/);
        const branchName = newSessionMatch?.[1] ?? checkoutMatch?.[1];

        if (branchName && pendingSessionSwitch.current) {
          // Capture and clear immediately — before any await — to close the race window
          // with chat_done (which may fire from the engine while we fetch the session list).
          pendingSessionSwitch.current = null;

          const wp = workspacePathRef.current;
          if (wp) {
            try {
              const newSessions = await invoke<string[]>("list_sessions", { workspacePath: wp });
              setSessions(newSessions);
            } catch { /* ignore, still apply the switch below */ }
            setCurrentSession(branchName);
            invoke("save_session_config", { name: branchName });
          }
          sessionsBeforeNew.current = null;
        }

        setLines((prev) => [...prev, { role: "system", content }]);
        break;
      }
      case "error":
        setLines((prev) => [...prev, { role: "system", content: `[error] ${msg.content}` }]);
        break;
      case "tool_start": {
        if (msg.name === "run_command") {
          const args = msg.args as Record<string, unknown> | undefined;
          const command =
            typeof args?.command === "string"
              ? args.command
              : JSON.stringify(args ?? {});
          setTerminalLog((prev) => [
            ...prev,
            { command, output: "", returncode: null, timestamp: Date.now() },
          ]);
        }
        break;
      }
      case "tool_result": {
        if (msg.name === "run_command") {
          const result = msg.result as Record<string, unknown> | undefined;
          const output = typeof result?.output === "string" ? result.output : "";
          const returncode =
            typeof result?.returncode === "number" ? result.returncode : null;
          setTerminalLog((prev) => {
            if (prev.length === 0) return prev;
            const copy = [...prev];
            copy[copy.length - 1] = { ...copy[copy.length - 1], output, returncode };
            return copy;
          });
        }
        break;
      }
      default:
        break;
    }
  }

  function handleSend() {
    if (!input.trim() || !engineReady) return;
    setLines((prev) => [...prev, { role: "user", content: input }]);
    sendToEngine({ type: "chat", content: input });
    setInput("");
  }

  function handleEditDecision(editId: string, accepted: boolean) {
    sendToEngine({ type: "edit_decision", editId, accepted });
    setLines((prev) => [
      ...prev,
      { role: "system", content: accepted ? "Edit accepted." : "Edit rejected." },
    ]);
    setPendingEdit(null);
  }

  // ── Session restore on boot ─────────────────────────────────────────
  useEffect(() => {
    if (!engineReady || !workspacePath) return;

    async function restoreSession() {
      try {
        const list = await invoke<string[]>("list_sessions", { workspacePath });
        setSessions(list);

        const saved = await invoke<string | null>("load_session_config");
        if (saved && saved !== "main" && list.includes(saved)) {
          pendingSessionSwitch.current = saved;
          sendToEngine({ type: "chat", content: `/checkout ${saved}` });
          setLines((prev) => [...prev, { role: "system", content: `Restoring session: ${saved}` }]);
        }
      } catch (e) {
        console.error("session restore failed", e);
      }
    }

    restoreSession();
  }, [engineReady]);

  function handleSessionSelect(name: string) {
    if (name === currentSession) return;
    setSessionDropdownOpen(false);
    pendingSessionSwitch.current = name;
    sessionsBeforeNew.current = null;
    sendToEngine({ type: "chat", content: `/checkout ${name}` });
    setLines((prev) => [...prev, { role: "system", content: `Switching to session: ${name}...` }]);
  }

  function handleNewSession() {
    setSessionDropdownOpen(false);
    if (workspacePath) {
      invoke<string[]>("list_sessions", { workspacePath }).then((list) => {
        sessionsBeforeNew.current = list;
      });
    }
    pendingSessionSwitch.current = "__new__";
    sendToEngine({ type: "chat", content: "/branch" });
    setLines((prev) => [...prev, { role: "system", content: "Creating new session..." }]);
  }

  async function handleFileClick(path: string) {
    if (editorDirty && !(await confirm("Discard unsaved changes?"))) return;
    try {
      const content = await invoke<string>("read_file_contents", { path });
      setOpenFile({ path, content });
      setEditorDirty(false);
    } catch (e) {
      setLines((prev) => [...prev, { role: "system", content: `[failed to open file] ${e}` }]);
    }
  }

  // ── Workspace picker screen (shown when no path saved) ────────────────
  if (!workspaceLoading && !workspacePath) {
    return (
      <div className="app-shell workspace-picker-screen">
        <main className="center-panel workspace-picker-center">
          <div className="workspace-picker-card">
            <h1>Welcome to Kyrex IDE</h1>
            <p>Select a project folder to get started.</p>
            <button className="workspace-picker-btn" onClick={handleSelectWorkspace}>
              Choose Folder
            </button>
          </div>
        </main>
      </div>
    );
  }

  // ── Setup wizard gate ────────────────────────────────────────────────
  if (needsSetup === true && configPath) {
    return (
      <SetupWizard
        configPath={configPath}
        onComplete={() => {
          setNeedsSetup(false);
        }}
      />
    );
  }
  if (needsSetup === null) {
    return <div className="setup-loading">Checking configuration...</div>;
  }

  // ── Main app layout ──────────────────────────────────────────────────
  return (
    <div className="app-shell">
      {sidebarOpen && (
        <aside className="file-tree">
          <div className="panel-header">
            <div className="session-selector" onClick={() => setSessionDropdownOpen((v) => !v)}>
              <span className="session-selector-label">{currentSession}</span>
              <span className="session-chevron">{sessionDropdownOpen ? "▾" : "▸"}</span>
              {sessionDropdownOpen && (
                <div className="session-dropdown">
                  {sessions.map((s) => (
                    <div
                      key={s}
                      className={`session-option${s === currentSession ? " session-active" : ""}`}
                      onClick={(e) => { e.stopPropagation(); handleSessionSelect(s); }}
                    >
                      {s}
                    </div>
                  ))}
                  <div className="session-divider" />
                  <div
                    className="session-option session-new"
                    onClick={(e) => { e.stopPropagation(); handleNewSession(); }}
                  >
                    + New Session
                  </div>
                </div>
              )}
            </div>
            <div className="panel-header-actions">
              <button
                className="terminal-toggle-btn"
                onClick={() => setTerminalOpen((v) => !v)}
                title="Toggle terminal output"
              >
                &gt;_
              </button>
              <button
                className="race-toggle-btn"
                onClick={() => setRaceViewOpen((v) => !v)}
                title="Race Mode"
              >
                🏁
              </button>
              <button
                className="cloud-toggle-btn"
                onClick={() => setCloudViewOpen((v) => !v)}
                title="Kyrex Cloud tasks"
              >
                ☁
              </button>
              <button className="change-workspace-btn" onClick={handleSelectWorkspace} title="Change workspace folder">
                📁
              </button>
              <button
                className="settings-toggle-btn"
                onClick={() => setSettingsOpen((v) => !v)}
                title="Settings"
              >
                ⚙
              </button>
              <button onClick={() => setSidebarOpen(false)}>«</button>
            </div>
          </div>
          {settingsOpen && (
            <SettingsPanel
              autoApprove={autoApprove}
              setAutoApprove={setAutoApprove}
              autoApproveDelay={autoApproveDelay}
              setAutoApproveDelay={setAutoApproveDelay}
              onClose={() => setSettingsOpen(false)}
            />
          )}
          {workspacePath ? (
            <FileTree rootPath={workspacePath} onFileClick={handleFileClick} />
          ) : (
            <div className="tree-loading">Resolving workspace...</div>
          )}
          <div className="sidebar-version">Kyrex IDE v{appVersion}</div>
        </aside>
      )}

      <main className="center-panel">
        {!sidebarOpen && (
          <button className="open-sidebar-btn" onClick={() => setSidebarOpen(true)}>
            »
          </button>
        )}

        {cloudViewOpen ? (
          <CloudPanel onClose={() => setCloudViewOpen(false)} />
        ) : raceViewOpen ? (
          <RaceView workspacePath={workspacePath ?? ""} onClose={() => setRaceViewOpen(false)} />
        ) : pendingEdit ? (
          <EditApproval edit={pendingEdit} onDecision={handleEditDecision} autoApprove={autoApprove} autoApproveDelay={autoApproveDelay} />
        ) : openFile ? (
          <CodeEditor
            key={openFile.path}
            filePath={openFile.path}
            content={openFile.content}
            onDirtyChange={setEditorDirty}
            onClose={() => setOpenFile(null)}
          />
        ) : (
          <div className="agent-chat">
            <div className="chat-messages">
              {lines.map((line, i) => (
                <div key={i} className={`msg ${line.role}`}>
                  {line.content}
                </div>
              ))}
            </div>
            <div className="chat-input-bar">
              <input
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleSend()}
                placeholder={engineReady ? "Ask Kyrex..." : "Waiting for engine..."}
                disabled={!engineReady}
              />
              <button onClick={handleSend} disabled={!engineReady}>
                Send
              </button>
            </div>
          </div>
        )}
      </main>

      {terminalOpen && (
        <TerminalPanel log={terminalLog} onClose={() => setTerminalOpen(false)} />
      )}
    </div>
  );
}
