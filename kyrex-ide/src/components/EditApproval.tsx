import { useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";

export interface ProposedEdit {
  editId: string;
  filePath: string;
  content: string;
}

interface Props {
  edit: ProposedEdit;
  onDecision: (editId: string, accepted: boolean) => void;
}

export default function EditApproval({ edit, onDecision }: Props) {
  const [oldContent, setOldContent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    invoke<string>("read_file_contents", { path: edit.filePath })
      .then((contents) => {
        if (!cancelled) setOldContent(contents);
      })
      .catch((e) => {
        if (!cancelled) setError(String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [edit.filePath]);

  const isNewFile = oldContent === "";

  return (
    <div className="edit-approval">
      <div className="edit-approval-header">
        <span className="edit-approval-path">{edit.filePath}</span>
        {isNewFile && <span className="edit-approval-badge">new file</span>}
      </div>

      {error && <div className="edit-approval-error">Failed to read current file: {error}</div>}

      <div className="edit-approval-diff">
        <div className="diff-pane">
          <div className="diff-pane-label">Current</div>
          <pre>{oldContent === null ? "Loading..." : oldContent || "(empty / new file)"}</pre>
        </div>
        <div className="diff-pane">
          <div className="diff-pane-label">Proposed</div>
          <pre>{edit.content}</pre>
        </div>
      </div>

      <div className="edit-approval-actions">
        <button className="reject-btn" onClick={() => onDecision(edit.editId, false)}>
          Reject
        </button>
        <button className="accept-btn" onClick={() => onDecision(edit.editId, true)}>
          Accept
        </button>
      </div>
    </div>
  );
}
