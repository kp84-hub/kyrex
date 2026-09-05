import { useEffect, useRef, useState } from "react";

/**
 * A `confirm_request` rendered by the IDE. Mirrors the engine protocol used
 * by the TUI: non-deletion gates may auto-approve only when the user has
 * enabled auto-approve; deletion gates ALWAYS require an explicit human
 * decision (never auto-approved), matching tui/update_engine.go.
 */
export interface ConfirmRequest {
  id: string;
  value: string; // "" | "edit" | "deletion" | ...
  path?: string; // display text (may be "DELETE: ...")
  paths?: string[]; // real resolved deletion targets
  diff?: string;
}

interface Props {
  confirm: ConfirmRequest;
  onDecision: (id: string, approved: boolean) => void;
  autoApprove?: boolean;
  autoApproveDelay?: number;
}

export default function ConfirmApproval({
  confirm,
  onDecision,
  autoApprove = false,
  autoApproveDelay = 5,
}: Props) {
  const [countdown, setCountdown] = useState<number | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const isDeletion = confirm.value === "deletion";
  // Safety: deletion confirmations are NEVER auto-approved.
  const allowAuto = autoApprove && !isDeletion;

  useEffect(() => {
    if (!allowAuto) {
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
        onDecision(confirm.id, true);
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
  }, [allowAuto, autoApproveDelay, confirm.id, onDecision]);

  function decide(approved: boolean) {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
    onDecision(confirm.id, approved);
  }

  const targetSummary =
    isDeletion && confirm.paths && confirm.paths.length
      ? confirm.paths.join("\n")
      : confirm.diff || confirm.path || "(no details)";

  return (
    <div className="edit-approval">
      <div className="edit-approval-header">
        <span className="edit-approval-path">{confirm.path || "Confirmation"}</span>
        {isDeletion && <span className="edit-approval-badge">deletion</span>}
        {allowAuto && countdown !== null && (
          <span className="auto-approve-countdown">
            {countdown > 0
              ? `Auto-approving in ${countdown}...`
              : "Approving now!"}
          </span>
        )}
      </div>

      <div className="edit-approval-diff">
        <div className="diff-pane">
          <div className="diff-pane-label">
            {isDeletion ? "Deletion targets" : "Proposed change"}
          </div>
          <pre>{targetSummary}</pre>
        </div>
      </div>

      <div className="edit-approval-actions">
        <button className="reject-btn" onClick={() => decide(false)}>
          {isDeletion ? "Deny" : "Reject"}
        </button>
        <button className="accept-btn" onClick={() => decide(true)}>
          {isDeletion ? "Approve deletion" : "Accept"}
        </button>
      </div>
    </div>
  );
}