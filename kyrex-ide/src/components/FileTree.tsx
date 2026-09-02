import { useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";

interface FileEntry {
  name: string;
  path: string;
  is_dir: boolean;
}

interface NodeProps {
  entry: FileEntry;
  depth: number;
  onFileClick: (path: string) => void;
  selectedPath: string | null;
  collapseSignal: number;
}

function TreeNode({ entry, depth, onFileClick, selectedPath, collapseSignal }: NodeProps) {
  const [expanded, setExpanded] = useState(false);
  const [children, setChildren] = useState<FileEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setExpanded(false);
  }, [collapseSignal]);

  async function handleClick() {
    if (entry.is_dir) {
      if (!expanded && children === null) {
        try {
          const result = await invoke<FileEntry[]>("list_dir", { path: entry.path });
          setChildren(result);
        } catch (e) {
          setError(String(e));
        }
      }
      setExpanded((prev) => !prev);
    } else {
      onFileClick(entry.path);
    }
  }

  return (
    <div>
      <div
        className={`tree-node${selectedPath === entry.path ? " tree-node-selected" : ""}`}
        style={{ paddingLeft: depth * 14 }}
        onClick={handleClick}
      >
        <span className="tree-icon">{entry.is_dir ? (expanded ? "▾" : "▸") : "  "}</span>
        <span className="tree-name">{entry.name}</span>
      </div>
      {error && (
        <div className="tree-error" style={{ paddingLeft: (depth + 1) * 14 }}>
          {error}
        </div>
      )}
      {expanded && children && (
        <div>
          {children.map((child) => (
            <TreeNode key={child.path} entry={child} depth={depth + 1} onFileClick={onFileClick} selectedPath={selectedPath} collapseSignal={collapseSignal} />
          ))}
        </div>
      )}
    </div>
  );
}

interface FileTreeProps {
  rootPath: string;
  onFileClick: (path: string) => void;
  selectedPath?: string | null;
}

export default function FileTree({ rootPath, onFileClick, selectedPath = null }: FileTreeProps) {
  const [entries, setEntries] = useState<FileEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [collapseAll, setCollapseAll] = useState(0);

  useEffect(() => {
    let cancelled = false;
    invoke<FileEntry[]>("list_dir", { path: rootPath })
      .then((result) => {
        if (!cancelled) setEntries(result);
      })
      .catch((e) => {
        if (!cancelled) setError(String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [rootPath]);

  if (error) return <div className="tree-error">{error}</div>;
  if (entries === null) return <div className="tree-loading">Loading...</div>;

  return (
    <div className="file-tree-root">
      <div className="file-tree-toolbar">
        <button
          className="collapse-all-btn"
          onClick={() => setCollapseAll((c) => c + 1)}
          title="Collapse all folders"
        >
          ▴
        </button>
      </div>
      {entries.map((entry) => (
        <TreeNode key={entry.path} entry={entry} depth={0} onFileClick={onFileClick} selectedPath={selectedPath} collapseSignal={collapseAll} />
      ))}
    </div>
  );
}
