import { useEffect, useState, useRef } from "react";
import { invoke } from "@tauri-apps/api/core";

export interface ProposedEdit {
  editId: string;
  filePath: string;
  content: string;
}

interface Props {
  edit: ProposedEdit;
  onDecision: (editId: string, accepted: boolean) => void;
  autoApprove?: boolean;
  autoApproveDelay?: number;
}

export default function EditApproval({ edit, onDecision, autoApprove, autoApproveDelay = 5 }: Props) {
  const [oldContent, setOldContent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [countdown, setCountdown] = useState<number | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // ── Auto-approve countdown timer ─────────────────────────────────
  useEffect(() => {
    if (!autoApprove) {
      if (timerRef.current) clearInterval(timerRef.current);
      setCountdown(null);
      return;
    }

    let remaining = autoApproveDelay;
    setCountdown(remaining);

    timerRef.current = setInterval(() => {
      remaining -= 1;
      if (remaining <= 0) {
        if (timerRef.current) clearInterval(timerRef.current);
        timerRef.current = null;
        setCountdown(0);
        // Auto-accept — flush the pending decision
        onDecision(edit.editId, true);
      } else {
        setCountdown(remaining);
      }
    }, 1000);

    return () => {
      if (timerRef.current) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [autoApprove, autoApproveDelay, edit.editId, onDecision]);

  function handleAccept() {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
    onDecision(edit.editId, true);
  }

  function handleReject() {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
    onDecision(edit.editId, false);
  }

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
        {autoApprove && countdown !== null && (
          <span className="auto-approve-countdown">
            {countdown > 0
              ? `Auto-approving in ${countdown}...`
              : "Approving now!"}
          </span>
        )}
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
        <button className="reject-btn" onClick={handleReject}>
          Reject
        </button>
        <button className="accept-btn" onClick={handleAccept}>
          Accept
        </button>
      </div>
    </div>
  );
}
