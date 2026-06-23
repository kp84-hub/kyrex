import Editor from "@monaco-editor/react";

interface Props {
  filePath: string;
  content: string;
  onClose: () => void;
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

export default function CodeEditor({ filePath, content, onClose }: Props) {
  return (
    <div className="editor-pane">
      <div className="panel-header">
        <span>{filePath}</span>
        <button onClick={onClose}>Back to chat</button>
      </div>
      <div className="monaco-wrapper">
        <Editor
          height="100%"
          theme="vs-dark"
          language={languageFromPath(filePath)}
          value={content}
          options={{ readOnly: true, minimap: { enabled: false } }}
        />
      </div>
    </div>
  );
}
