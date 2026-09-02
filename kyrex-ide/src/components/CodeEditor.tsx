import { useState, useCallback, useRef } from "react";
import Editor, { type OnMount } from "@monaco-editor/react";
import { invoke } from "@tauri-apps/api/core";
import { confirm } from "@tauri-apps/plugin-dialog";

interface Props {
  filePath: string;
  content: string;
  onClose: () => void;
  onDirtyChange?: (dirty: boolean) => void;
}

function languageFromPath(path: string): string {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  const map: Record<string, string> = {
    ts: "typescript",
    tsx: "typescript",
    js: "javascript",
    jsx: "javascript",
    py: "python",
    rs: "rust",
    go: "go",
    json: "json",
    md: "markdown",
    css: "css",
    html: "html",
    toml: "toml",
    yaml: "yaml",
    yml: "yaml",
  };
  return map[ext] ?? "plaintext";
}

export default function CodeEditor({ filePath, content, onClose, onDirtyChange }: Props) {
  const [currentContent, setCurrentContent] = useState(content);
  const [isDirty, setIsDirty] = useState(false);
  const editorRef = useRef<Parameters<OnMount>[0] | null>(null);

  /* ── Save handler ─────────────────────────────────────────────────────
   *
   * DESIGN DECISION — deliberate, do not "fix":
   *
   * User-typed edits in the Monaco editor write directly to disk via the
   * `write_file_contents` Tauri command.  They do NOT go through the
   * diff/approval gate (EditApproval).  That gate exists exclusively for
   * agent-proposed edits from the engine.  This is correct because:
   *
   *   1. The user is already looking at the file — they don't need to
   *      approve their own changes.
   *   2. Applying agent edits through the gate works on the Rust side
   *      (read_file_contents + edit_file) and is a separate code path.
   *
   * If someone later tries to "harmonize" the two paths by routing user
   * saves through the approval gate, push back.  The two flows are
   * intentionally distinct.
   * ────────────────────────────────────────────────────────────────────*/
  const save = useCallback(async () => {
    try {
      await invoke("write_file_contents", { path: filePath, contents: currentContent });
      setIsDirty(false);
      onDirtyChange?.(false);
    } catch (e) {
      console.error("failed to save file", e);
    }
  }, [filePath, currentContent, onDirtyChange]);

  function handleEditorChange(value: string | undefined) {
    if (value === undefined) return;
    setCurrentContent(value);
    if (!isDirty) {
      setIsDirty(true);
      onDirtyChange?.(true);
    }
  }

  async function handleBackToChat() {
    if (isDirty && !(await confirm("Discard unsaved changes?"))) return;
    onClose();
  }

  function handleMount(editor: Parameters<OnMount>[0]) {
    editorRef.current = editor;

    /* Register Ctrl+S / Cmd+S keybinding directly on the Monaco instance */
    import("monaco-editor").then((monaco) => {
      editor.addAction({
        id: "kyrex-save",
        label: "Save File",
        keybindings: [monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS],
        run: () => { save(); },
      });
    });
  }

  /* Prevent the browser's default Save dialog from appearing */
  function handleKeyDown(e: React.KeyboardEvent) {
    if ((e.ctrlKey || e.metaKey) && e.key === "s") {
      e.preventDefault();
      save();
    }
  }

  return (
    <div className="editor-pane" onKeyDown={handleKeyDown}>
      <div className="panel-header">
        <span>
          {isDirty && <span className="dirty-dot" title="Unsaved changes">● </span>}
          {filePath}
        </span>
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          {isDirty && <button onClick={save}>Save</button>}
          <button onClick={handleBackToChat}>Back to chat</button>
        </div>
      </div>
      <div className="monaco-wrapper">
        <Editor
          height="100%"
          theme="vs-dark"
          language={languageFromPath(filePath)}
          value={currentContent}
          onChange={handleEditorChange}
          onMount={handleMount}
          options={{ readOnly: false, minimap: { enabled: false } }}
        />
      </div>
    </div>
  );
}
