interface Props {
  autoApprove: boolean;
  setAutoApprove: (v: boolean) => void;
  autoApproveDelay: number;
  setAutoApproveDelay: (v: number) => void;
  onClose: () => void;
}

export default function SettingsPanel({
  autoApprove,
  setAutoApprove,
  autoApproveDelay,
  setAutoApproveDelay,
  onClose,
}: Props) {
  return (
    <div className="settings-panel">
      <div className="settings-panel-header">
        <span>Settings</span>
        <button onClick={onClose}>×</button>
      </div>
      <div className="settings-panel-body">
        <div className="settings-row">
          <label className="auto-approve-toggle">
            <input
              type="checkbox"
              checked={autoApprove}
              onChange={(e) => setAutoApprove(e.target.checked)}
            />
            <span>Auto-approve edits</span>
          </label>
        </div>
        <div className="settings-row">
          <span className="settings-row-label">Delay before auto-approving</span>
          <div className="settings-delay-input">
            <input
              type="number"
              min={1}
              max={60}
              value={autoApproveDelay}
              onChange={(e) => setAutoApproveDelay(Math.max(1, Number(e.target.value)))}
              disabled={!autoApprove}
            />
            <span>seconds</span>
          </div>
        </div>
      </div>
    </div>
  );
}
