import { useState } from "react";
import "./App.css";

export default function App() {
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [editorOpen, setEditorOpen] = useState(false);

  return (
    <div className="app-shell">
      {sidebarOpen && (
        <aside className="file-tree">
          <div className="panel-header">
            <span>Files</span>
            <button onClick={() => setSidebarOpen(false)}>«</button>
          </div>
          <div className="file-tree-stub">(file tree goes here)</div>
        </aside>
      )}

      <main className="center-panel">
        {!sidebarOpen && (
          <button className="open-sidebar-btn" onClick={() => setSidebarOpen(true)}>
            »
          </button>
        )}

        {editorOpen ? (
          <div className="editor-pane">
            <div className="panel-header">
              <span>editor (review mode)</span>
              <button onClick={() => setEditorOpen(false)}>Back to chat</button>
            </div>
            <div className="editor-stub">(Monaco goes here)</div>
          </div>
        ) : (
          <div className="agent-chat">
            <div className="chat-messages">
              <div className="msg agent">Hi — I'm Kyrex. What are we building?</div>
            </div>
            <div className="chat-input-bar">
              <input placeholder="Ask Kyrex..." />
              <button onClick={() => setEditorOpen(true)}>Open editor (stub)</button>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
