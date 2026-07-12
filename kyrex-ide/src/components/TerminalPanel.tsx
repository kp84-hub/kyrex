import { useEffect, useRef } from "react";

interface TerminalEntry {
  command: string;
  output: string;
  returncode: number | null;
  timestamp: number;
}

interface Props {
  log: TerminalEntry[];
  onClose: () => void;
}

export default function TerminalPanel({ log, onClose }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [log]);

  return (
    <aside className="terminal-panel">
      <div className="panel-header">
        <span>Terminal</span>
        <button onClick={onClose}>×</button>
      </div>
      <div className="terminal-feed">
        {log.length === 0 && (
          <div className="terminal-empty">No commands executed yet.</div>
        )}
        {log.map((entry, i) => (
          <div key={i} className="terminal-entry">
            <div className="terminal-prompt">
              <span className="terminal-chevron">$</span> {entry.command}
            </div>
            {entry.output && (
              <pre className="terminal-output">{entry.output}</pre>
            )}
            {entry.returncode !== null && (
              <div
                className={`terminal-exit ${entry.returncode === 0 ? "exit-ok" : "exit-err"}`}
              >
                ❯ Exit code: {entry.returncode}
              </div>
            )}
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
    </aside>
  );
}