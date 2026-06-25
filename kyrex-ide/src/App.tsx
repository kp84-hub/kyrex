import { useState, useRef, useEffect } from "react";
import "./App.css";
import { invoke } from "@tauri-apps/api/core";
import { homeDir } from "@tauri-apps/api/path";
import { startEngine, sendToEngine, type EngineMessage } from "./lib/engineClient";
import EditApproval, { type ProposedEdit } from "./components/EditApproval";
import FileTree from "./components/FileTree";
import CodeEditor from "./components/CodeEditor";

interface ChatLine {
  role: "user" | "agent" | "system";
  content: string;
}

export default function App() {
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [openFile, setOpenFile] = useState<{ path: string; content: string } | null>(null);
  const [lines, setLines] = useState<ChatLine[]>([
    { role: "system", content: "Starting engine..." },
  ]);
  const [input, setInput] = useState("");
  const [engineReady, setEngineReady] = useState(false);
  const [pendingEdit, setPendingEdit] = useState<ProposedEdit | null>(null);
  const [workspacePath, setWorkspacePath] = useState<string | null>(null);
  const streamingRef = useRef<string>("");
  const isStreamingRef = useRef<boolean>(false);

  useEffect(() => {
    let cancelled = false;

    async function boot() {
      let resolvedPath: string;
      try {
        resolvedPath = await homeDir();
      } catch (e) {
        if (!cancelled) {
          setLines((prev) => [...prev, { role: "system", content: `[failed to resolve home dir] ${e}` }]);
        }
        return;
      }
      if (cancelled) return;
      setWorkspacePath(resolvedPath);

      try {
        await startEngine(
          resolvedPath,
          (msg: EngineMessage) => {
            if (cancelled) return;
            handleMessage(msg);
          },
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
        if (!cancelled) {
          setEngineReady(true);
          setLines((prev) => [...prev, { role: "system", content: "Engine ready." }]);
        }
      } catch (e) {
        setLines((prev) => [...prev, { role: "system", content: `[failed to start engine] ${e}` }]);
      }
    }

    boot();
    return () => {
      cancelled = true;
    };
  }, []);

  function handleMessage(msg: EngineMessage) {
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
      case "chat_done":
        streamingRef.current = "";
        isStreamingRef.current = false;
        break;
      case "propose_edit": {
        const editId = msg.editId as string;
        const filePath = msg.filePath as string;
        const content = (msg.content as string) ?? "";
        setPendingEdit({ editId, filePath, content });
        break;
      }
      case "error":
        setLines((prev) => [...prev, { role: "system", content: `[error] ${msg.content}` }]);
        break;
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

  async function handleFileClick(path: string) {
    try {
      const content = await invoke<string>("read_file_contents", { path });
      setOpenFile({ path, content });
    } catch (e) {
      setLines((prev) => [...prev, { role: "system", content: `[failed to open file] ${e}` }]);
    }
  }

  return (
    <div className="app-shell">
      {sidebarOpen && (
        <aside className="file-tree">
          <div className="panel-header">
            <span>Files</span>
            <button onClick={() => setSidebarOpen(false)}>«</button>
          </div>
          {workspacePath ? (
            <FileTree rootPath={workspacePath} onFileClick={handleFileClick} />
          ) : (
            <div className="tree-loading">Resolving workspace...</div>
          )}
        </aside>
      )}

      <main className="center-panel">
        {!sidebarOpen && (
          <button className="open-sidebar-btn" onClick={() => setSidebarOpen(true)}>
            »
          </button>
        )}

        {pendingEdit ? (
          <EditApproval edit={pendingEdit} onDecision={handleEditDecision} />
        ) : openFile ? (
          <CodeEditor
            filePath={openFile.path}
            content={openFile.content}
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
    </div>
  );
}
