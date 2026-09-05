import { useState, useRef, useEffect, useCallback } from "react";
import "./App.css";
import { invoke } from "@tauri-apps/api/core";
import { open, confirm } from "@tauri-apps/plugin-dialog";
import { startEngine, sendToEngine, type EngineMessage } from "./lib/engineClient";
import EditApproval, { type ProposedEdit } from "./components/EditApproval";
import ConfirmApproval, { type ConfirmRequest } from "./components/ConfirmApproval";
import FileTree from "./components/FileTree";
import CodeEditor from "./components/CodeEditor";
import TerminalPanel from "./components/TerminalPanel";
import SettingsPanel from "./components/SettingsPanel";
import RaceView from "./components/RaceView";
import SetupWizard from "./components/SetupWizard";
import { homeDir, join } from "@tauri-apps/api/path";
import { getVersion } from "@tauri-apps/api/app";
import { FluxTaskClient, type FluxConnectionState, type FluxEvent } from "./lib/fluxClient";
import { CloudAuthClient, type CloudAuthState } from "./lib/cloudAuth";
import FluxPanel from "./components/FluxPanel";

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
  const [activeActivity, setActiveActivity] = useState("explorer");
  const [bottomTab, setBottomTab] = useState("terminal");
  const [openFiles, setOpenFiles] = useState<{ path: string; content: string }[]>([]);
  const [activeFilePath, setActiveFilePath] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<string[]>([]);
  const [searching, setSearching] = useState(false);
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
  const assistantMessagesRef = useRef<HTMLDivElement | null>(null);
  const followAssistantMessagesRef = useRef(true);

  useEffect(() => {
    const messages = assistantMessagesRef.current;
    if (messages && followAssistantMessagesRef.current) {
      messages.scrollTop = messages.scrollHeight;
    }
  }, [lines]);
  const [terminalOpen, setTerminalOpen] = useState(false);
  const [terminalLog, setTerminalLog] = useState<TerminalEntry[]>([]);
  const [outputLog, setOutputLog] = useState<string[]>([]);
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
  const [pendingConfirm, setPendingConfirm] = useState<ConfirmRequest | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [raceViewOpen, setRaceViewOpen] = useState(false);
  const [fluxTaskId, setFluxTaskId] = useState<string | null>(null);
  const [fluxStatus, setFluxStatus] = useState("idle");
  const [fluxConnection, setFluxConnection] = useState<FluxConnectionState>("idle");
  const [fluxEvents, setFluxEvents] = useState<FluxEvent[]>([]);
  const [fluxClient, setFluxClient] = useState<FluxTaskClient | null>(null);
  const fluxClientRef = useRef<FluxTaskClient | null>(null);
  const [cloudUser, setCloudUser] = useState<string | null>(null);
  const [cloudAuthState, setCloudAuthState] = useState<CloudAuthState>("checking");
  const [fluxError, setFluxError] = useState<string | null>(null);
  const CLOUD_URL = import.meta.env.VITE_KYREX_CLOUD_URL ?? "https://kyrex-cloud.example";
  const cloudConfigured = Boolean(import.meta.env.VITE_KYREX_CLOUD_URL);
  const authRef = useRef<CloudAuthClient | null>(null);

  useEffect(() => {
    const auth = new CloudAuthClient(CLOUD_URL);
    authRef.current = auth;
    const unsubscribe = auth.subscribe(() => { setCloudUser(auth.user); setCloudAuthState(auth.authState); });
    auth.initialize().catch(() => setOutputLog((prev) => [...prev, "[auth] Cloud authentication unavailable"]));
    return () => { unsubscribe(); auth.dispose(); authRef.current = null; };
  }, []);

  async function handleDesktopLogin() {
    try { await authRef.current?.signIn(); }
    catch (error) { setOutputLog((prev) => [...prev, `[auth] ${String(error)}`]); }
  }

  async function handleDesktopLogout() { await authRef.current?.signOut(); }

  useEffect(() => {
    getVersion().then(setAppVersion).catch(() => setAppVersion(""));
    const client = new FluxTaskClient({
      baseUrl: CLOUD_URL,
      request: (path, init) => authRef.current?.request(path, init) ?? fetch(path, init),
      onConnection: setFluxConnection,
      onEvent: (event) => {
        setFluxEvents((prev) => [...prev, event]);
        const status = typeof event.payload.status === "string" ? event.payload.status : null;
        if (status) setFluxStatus(status);
        if (event.type === "end") setFluxStatus(status ?? "complete");
      },
      onError: (error) => { setFluxError(error.message); setOutputLog((prev) => [...prev, `[cloud] ${error.message}`]); },
    });
    fluxClientRef.current = client;
    setFluxClient(client);
    return () => {
      client.dispose();
      fluxClientRef.current = null;
      setFluxClient(null);
    };
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
      case "confirm_request": {
        setPendingConfirm({
          id: String(msg.id ?? ""),
          value: String(msg.value ?? ""),
          path: typeof msg.path === "string" ? msg.path : undefined,
          paths: Array.isArray(msg.paths) ? (msg.paths as string[]) : undefined,
          diff: typeof msg.diff === "string" ? msg.diff : undefined,
        });
        break;
      }
      case "system": {
        const content = msg.content ?? "";
        setOutputLog((prev) => [...prev, content]);

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

  async function handleCloudTask() {
    const task = input.trim();
    if (!task) return;
    const client = fluxClientRef.current;
    if (!client) return;
    try {
      const snapshot = await client.submit(task);
      setFluxTaskId(snapshot.task_id);
      setFluxStatus(snapshot.status);
      setFluxEvents([]);
      setFluxError(null);
      setBottomTab("tasks");
      setTerminalOpen(true);
      setInput("");
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setFluxError(message);
      setOutputLog((prev) => [...prev, `[cloud] ${message}`]);
    }
  }

  // ── Flux workspace handlers (shared single FluxTaskClient instance) ───
  async function handleFluxSubmit(task: string) {
    const client = fluxClientRef.current;
    if (!client || !task.trim()) return;
    try {
      setFluxError(null);
      const snapshot = await client.submit(task.trim());
      setFluxTaskId(snapshot.task_id);
      setFluxStatus(snapshot.status);
      setFluxEvents([]);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setFluxError(message);
      setOutputLog((prev) => [...prev, `[cloud] ${message}`]);
    }
  }

  async function handleFluxCancel() {
    const client = fluxClientRef.current;
    if (!client || !fluxTaskId) return;
    try {
      await client.cancel(fluxTaskId);
      setOutputLog((prev) => [...prev, `[cloud] Cancel requested for ${fluxTaskId}`]);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setFluxError(message);
      setOutputLog((prev) => [...prev, `[cloud] ${message}`]);
    }
  }

  function handleFluxDismiss() {
    setFluxTaskId(null);
    setFluxStatus("");
    setFluxEvents([]);
    setFluxError(null);
  }

  function handleFluxClearError() {
    setFluxError(null);
  }

  function handleFluxReopen(taskId: string) {
    const client = fluxClientRef.current;
    if (!client) return;
    setFluxTaskId(taskId);
    setFluxStatus("");
    setFluxEvents([]);
    setFluxError(null);
    client.follow(taskId);
  }

  function handleEditDecision(editId: string, accepted: boolean) {
    sendToEngine({ type: "edit_decision", editId, accepted });
    setLines((prev) => [
      ...prev,
      { role: "system", content: accepted ? "Edit accepted." : "Edit rejected." },
    ]);
    setPendingEdit(null);
  }

  function handleConfirmResponse(id: string, approved: boolean) {
    sendToEngine({ type: "confirm_response", id, approved });
    setLines((prev) => [
      ...prev,
      { role: "system", content: approved ? "Change approved." : "Change rejected." },
    ]);
    setPendingConfirm(null);
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
    if (editorDirty && activeFilePath !== path && !(await confirm("Discard unsaved changes?"))) return;
    try {
      const existing = openFiles.find((file) => file.path === path);
      const file = existing ?? { path, content: await invoke<string>("read_file_contents", { path }) };
      setOpenFiles((prev) => existing ? prev : [...prev, file]);
      setOpenFile(file);
      setActiveFilePath(path);
      setEditorDirty(false);
      setActiveActivity("explorer");
    } catch (e) {
      setLines((prev) => [...prev, { role: "system", content: `[failed to open file] ${e}` }]);
    }
  }

  async function handleSearch() {
    const query = searchQuery.trim();
    if (!query || !workspacePath) return;
    setSearching(true);
    try {
      const result: string[] = [];
      const visit = async (path: string) => {
        if (result.length >= 50) return;
        const entries = await invoke<{ name: string; path: string; is_dir: boolean }[]>("list_dir", { path });
        for (const entry of entries) {
          if (result.length >= 50) break;
          if (entry.is_dir) await visit(entry.path);
          else if (entry.name.toLowerCase().includes(query.toLowerCase())) result.push(entry.path);
        }
      };
      await visit(workspacePath);
      setSearchResults(result);
    } catch (e) {
      setOutputLog((prev) => [...prev, `[search error] ${e}`]);
    } finally {
      setSearching(false);
    }
  }

  function closeFile(path: string) {
    const remaining = openFiles.filter((file) => file.path !== path);
    setOpenFiles(remaining);
    const next = remaining[remaining.length - 1] ?? null;
    setOpenFile(next);
    setActiveFilePath(next?.path ?? null);
    setEditorDirty(false);
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
  const activityItems = [
    { id: "explorer", icon: "▤", label: "Explorer" },
    { id: "search", icon: "⌕", label: "Search" },
    { id: "source", icon: "⑂", label: "Source Control" },
    { id: "run", icon: "▷", label: "Run / Tasks" },
    { id: "flux", icon: "≋", label: "Flux" },
    { id: "kyrex", icon: "✦", label: "Assistant" },
  ];
  const fileName = openFile?.path.split(/[\\/]/).pop() ?? "Welcome";

  return (
    <div className="app-shell">
      <header className="top-bar">
        <div className="brand-lockup"><span className="brand-mark">K</span><span>Kyrex IDE</span></div>
        <div className="workspace-title">{workspacePath?.split(/[\\/]/).pop() ?? "No workspace"}</div>
        <div className="top-actions">
          {cloudUser ? <button className="icon-button" onClick={handleDesktopLogout} title="Sign out of Kyrex Cloud">Cloud: {cloudUser} · Sign out</button> : <button className="icon-button" onClick={handleDesktopLogin} title="Sign in to Kyrex Cloud">Sign in</button>}
          <button className="top-session" onClick={() => setSessionDropdownOpen((v) => !v)}>{currentSession} <span>⌄</span></button>
          <button className="icon-button" onClick={() => setSettingsOpen((v) => !v)} title="Settings">⚙</button>
          <button className="icon-button" onClick={() => setRaceViewOpen((v) => !v)} title="Race Mode">◈</button>
        </div>
        {sessionDropdownOpen && (
          <div className="session-dropdown top-session-dropdown">
            {sessions.map((s) => <div key={s} className={`session-option${s === currentSession ? " session-active" : ""}`} onClick={() => handleSessionSelect(s)}>{s}</div>)}
            <div className="session-divider" />
            <div className="session-option session-new" onClick={handleNewSession}>+ New Session</div>
          </div>
        )}
      </header>

      <div className={`ide-body${sidebarOpen ? "" : " ide-body-sidebar-collapsed"}`}>
        <nav className="activity-bar" aria-label="Activity bar">
          <div className="activity-group">
            {activityItems.map((item) => (
              <button key={item.id} className={`activity-item${activeActivity === item.id ? " activity-active" : ""}`} onClick={() => { setActiveActivity(item.id); if (item.id === "kyrex") setSidebarOpen(true); if (item.id === "flux") setRaceViewOpen(false); }} title={item.label}>
                <span>{item.icon}</span><small>{item.label.split(" ")[0]}</small>
              </button>
            ))}
          </div>
          <button className="activity-item activity-bottom" onClick={() => setSettingsOpen((v) => !v)} title="Settings">⚙<small>Manage</small></button>
        </nav>

        {sidebarOpen && (
          <aside className="explorer-panel">
            <div className="explorer-heading"><span>EXPLORER</span><div className="panel-header-actions"><button onClick={handleSelectWorkspace} title="Change workspace">＋</button><button onClick={() => setSidebarOpen(false)} title="Hide explorer">‹</button></div></div>
            <div className="workspace-heading"><span className="workspace-chevron">⌄</span><span>EXPLORER</span></div>
            {settingsOpen && <SettingsPanel autoApprove={autoApprove} setAutoApprove={setAutoApprove} autoApproveDelay={autoApproveDelay} setAutoApproveDelay={setAutoApproveDelay} onClose={() => setSettingsOpen(false)} />}
            {activeActivity === "search" ? (
              <div className="activity-panel-content">
                <div className="activity-panel-title">SEARCH</div>
                <div className="search-box"><input value={searchQuery} onChange={(e) => setSearchQuery(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") handleSearch(); }} placeholder="Search files…" autoFocus /><button onClick={handleSearch} disabled={searching || !searchQuery.trim()}>⌕</button></div>
                <div className="search-results">{searching && <div className="activity-muted">Searching…</div>}{!searching && searchQuery && searchResults.length === 0 && <div className="activity-muted">No matching files</div>}{searchResults.map((path) => <button key={path} className="search-result" onClick={() => handleFileClick(path)}>{path.replace(`${workspacePath ?? ""}/`, "")}</button>)}</div>
              </div>
            ) : activeActivity === "source" ? (
              <div className="activity-panel-content"><div className="activity-panel-title">SOURCE CONTROL</div><div className="activity-empty"><span className="activity-empty-icon">⑂</span><b>Source control</b><span>Git actions are available through Kyrex tasks and the workspace terminal.</span></div></div>
            ) : activeActivity === "run" ? (
              <div className="activity-panel-content"><div className="activity-panel-title">RUN / TASKS</div><div className="task-summary"><span className="task-status-dot" />{terminalLog.length ? `${terminalLog.length} command${terminalLog.length === 1 ? "" : "s"} recorded` : "No active tasks"}</div><button className="activity-action" onClick={() => { setBottomTab("terminal"); setTerminalOpen(true); }}>Open task output</button></div>
            ) : activeActivity === "flux" ? (
              <div className="activity-panel-content"><div className="activity-panel-title">FLUX / BOTS</div><div className="activity-empty"><span className="activity-empty-icon">≋</span><b>Cloud task workspace</b><span>Use the Flux workspace to submit tasks, stream durable events, and reopen recent tasks.</span><button className="activity-action" onClick={() => setSidebarOpen(false)}>Open Flux workspace</button></div></div>
            ) : workspacePath ? <FileTree rootPath={workspacePath} onFileClick={handleFileClick} selectedPath={activeFilePath} /> : <div className="tree-loading">Resolving workspace...</div>}
            <div className="sidebar-version">Kyrex IDE {appVersion && `v${appVersion}`}</div>
          </aside>
        )}

        <main className="workspace-main">
          {!sidebarOpen && <button className="open-sidebar-btn" onClick={() => setSidebarOpen(true)}>›</button>}
          <div className="editor-region">
            <div className="editor-tabs">
              {openFiles.length ? openFiles.map((file) => { const name = file.path.split(/[\\/]/).pop() ?? file.path; return <div key={file.path} className={`editor-tab${activeFilePath === file.path ? " editor-tab-active" : ""}`} onClick={() => { setOpenFile(file); setActiveFilePath(file.path); }}><span className="file-type-dot">{name.endsWith(".tsx") || name.endsWith(".ts") ? "TS" : "{}"}</span><span>{name}</span>{activeFilePath === file.path && editorDirty && <span className="dirty-dot">●</span>}<button onClick={(e) => { e.stopPropagation(); closeFile(file.path); }} title="Close file">×</button></div>; }) : <div className="editor-tab editor-tab-active"><span className="file-type-dot">✦</span><span>Welcome</span></div>}
              <div className="editor-tab-spacer" />
              <button className="editor-action" onClick={() => { setBottomTab("terminal"); setTerminalOpen((v) => !v); }} title="Toggle terminal">⌄</button>
            </div>
            <div className="breadcrumbs">{workspacePath?.split(/[\\/]/).pop() ?? "workspace"}<span>/</span>{activeActivity === "flux" ? "Flux" : openFile ? fileName : "Welcome"}</div>
            <div className="editor-content">
              {raceViewOpen ? <RaceView workspacePath={workspacePath ?? ""} onClose={() => setRaceViewOpen(false)} /> : activeActivity === "flux" ? <FluxPanel authState={cloudAuthState} user={cloudUser} cloudUrl={CLOUD_URL} configured={cloudConfigured} client={fluxClient} taskId={fluxTaskId} status={fluxStatus} connection={fluxConnection} events={fluxEvents} error={fluxError} onSubmit={handleFluxSubmit} onCancel={handleFluxCancel} onDismiss={handleFluxDismiss} onClearError={handleFluxClearError} onReopen={handleFluxReopen} onSignIn={handleDesktopLogin} /> : pendingConfirm ? <ConfirmApproval confirm={pendingConfirm} onDecision={handleConfirmResponse} autoApprove={autoApprove} autoApproveDelay={autoApproveDelay} /> : pendingEdit ? <EditApproval edit={pendingEdit} onDecision={handleEditDecision} autoApprove={autoApprove} autoApproveDelay={autoApproveDelay} /> : openFile ? <CodeEditor key={openFile.path} filePath={openFile.path} content={openFile.content} onDirtyChange={setEditorDirty} onClose={() => setOpenFile(null)} /> : (
                <div className="welcome-view"><div className="welcome-mark">K</div><h1>Welcome to Kyrex</h1><p className="welcome-subtitle">An intelligent workspace for building software.</p><div className="welcome-actions"><button onClick={handleSelectWorkspace}><strong>＋</strong><span><b>Open Folder</b><small>Open a local project workspace</small></span></button><button onClick={() => { setActiveActivity("source"); setOutputLog((prev) => [...prev, "Clone Repository is not available in the current Tauri command surface."]); }}><strong>⌘</strong><span><b>Clone Repository</b><small>Use the workspace terminal to clone a project</small></span></button></div><div className="recent-heading">Recent Projects</div><div className="recent-project"><span className="project-icon">▣</span><span>{workspacePath?.split(/[\\/]/).pop() ?? "No recent projects"}</span><span className="project-path">{workspacePath ?? "Choose a folder to begin"}</span></div></div>
              )}
            </div>
          </div>

          <aside className={`assistant-panel${activeActivity === "kyrex" ? " assistant-focused" : ""}`}>
            <div className="assistant-header"><div><span className="assistant-icon">✦</span><span>ASSISTANT</span></div><span className={`connection-dot ${engineReady ? "connected" : ""}`} title={engineReady ? "Engine ready" : "Connecting"}>●</span></div>
            <div className="assistant-context"><span className="context-label">WORKSPACE</span><span className="context-value">{workspacePath?.split(/[\\/]/).pop() ?? "No folder open"}</span><span className="context-model">{engineReady ? "Engine ready" : "Connecting to engine"}</span></div>
            <div
          ref={assistantMessagesRef}
          className="assistant-messages"
          onScroll={(event) => {
            const messages = event.currentTarget;
            followAssistantMessagesRef.current =
              messages.scrollHeight - messages.scrollTop - messages.clientHeight <= 40;
          }}
        >{lines.map((line, i) => <div key={i} className={`assistant-message ${line.role}`}><span className="message-label">{line.role === "user" ? "You" : line.role === "agent" ? "Assistant" : "System"}</span><div>{line.content}</div></div>)}</div>
            <div className="assistant-composer"><textarea value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend(); } }} placeholder={engineReady ? "Ask Assistant to help…" : "Waiting for engine…"} disabled={!engineReady} rows={3} /><div className="composer-footer"><span>Shift + Enter for newline</span><div className="composer-actions"><button className="cloud-task-button" onClick={handleCloudTask} disabled={!input.trim() || fluxConnection === "connecting"}>Cloud task</button><button onClick={handleSend} disabled={!engineReady || !input.trim()}>Send <span>↵</span></button></div></div></div>
          </aside>
        </main>

        <section className={`bottom-panel${terminalOpen ? " bottom-expanded" : ""}`}>
          <div className="bottom-tabs">{["terminal", "output", "problems", "tasks"].map((tab) => <button key={tab} className={bottomTab === tab ? "bottom-tab-active" : ""} onClick={() => { setBottomTab(tab); setTerminalOpen(true); }}>{tab[0].toUpperCase() + tab.slice(1)}{tab === "problems" && <span className="tab-count">0</span>}</button>)}<button className="bottom-close" onClick={() => setTerminalOpen(false)}>⌄</button></div>{terminalOpen && <div className="bottom-content">{bottomTab === "terminal" ? <TerminalPanel log={terminalLog} onClose={() => setTerminalOpen(false)} /> : bottomTab === "output" ? <div className="output-feed">{outputLog.concat(fluxEvents.map((event) => `[cloud:${event.type}] ${JSON.stringify(event.payload)}`)).map((line, i) => <div key={i}>{line}</div>)}</div> : bottomTab === "tasks" ? <div className="task-feed"><div className="task-feed-header"><span>Cloud task</span><span className={`task-connection task-connection-${fluxConnection}`}>{fluxConnection}</span></div>{fluxTaskId ? <><div className="task-id">{fluxTaskId}</div><div className="task-status">Status: <b>{fluxStatus}</b></div><div className="task-events">{fluxEvents.map((event, i) => <div key={`${event.event_id ?? "synthetic"}-${i}`}><span>{event.type}</span>{event.event_id !== null && <small>#{event.event_id}</small>}</div>)}</div></> : <div className="empty-bottom">No active Cloud task</div>}</div> : <div className="empty-bottom">No problems detected</div>}</div>}
        </section>
      </div>

      <footer className="status-bar"><span className="status-branch">⑂ {currentSession}</span><span>{openFile ? (fileName.split(".").pop()?.toUpperCase() ?? "TEXT") : "Ready"}</span><span className="status-spacer" /><span className="status-engine"><i className={engineReady ? "status-online" : ""} />{engineReady ? "Engine ready" : "Connecting"}</span><span>Kyrex IDE</span></footer>
    </div>
  );
}
