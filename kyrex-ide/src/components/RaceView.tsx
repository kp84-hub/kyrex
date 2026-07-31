import { useState, useRef, useEffect } from "react";
import {
  startRace,
  killRace,
  mergeRaceLane,
  diff_race_lane,
  type RaceLaneInfo,
  type RaceLaneEvent,
} from "../lib/engineClient";

interface LaneState {
  id: number;
  model: string;
  dir: string;
  status: "pending" | "running" | "done" | "failed";
  tail: string[];
  lastTool: string | null;
  finalResponse: string | null;
  error: string | null;
}

interface LaneDiffInfo {
  diffText: string | null;
  diffLines: number;
  loadingDiff: boolean;
  diffExpanded: boolean;
  merging: boolean;
  mergeMessage: string | null;
}

interface Props {
  workspacePath: string;
  onClose: () => void;
}

const MAX_TAIL_LINES = 3;

export default function RaceView({ workspacePath, onClose }: Props) {
  const [phase, setPhase] = useState<"setup" | "racing" | "compare">("setup");
  const [task, setTask] = useState("");
  const [modelsInput, setModelsInput] = useState("");
  const [lanes, setLanes] = useState<LaneState[]>([]);
  const [starting, setStarting] = useState(false);
  const [setupError, setSetupError] = useState<string | null>(null);
  const [diffs, setDiffs] = useState<Record<number, LaneDiffInfo>>({});
  const [discarding, setDiscarding] = useState(false);
  const streamBuffers = useRef<Record<number, string>>({});
  const settledHandled = useRef(false);

  async function handleStart() {
    const models = modelsInput
      .split(",")
      .map((m) => m.trim())
      .filter((m) => m.length > 0);

    if (!task.trim()) {
      setSetupError("Enter a task for the race.");
      return;
    }
    if (models.length < 2) {
      setSetupError("Enter at least 2 comma-separated models to race.");
      return;
    }
    if (models.length > 4) {
      setSetupError("Race supports up to 4 models at a time.");
      return;
    }

    setSetupError(null);
    setStarting(true);
    try {
      const laneInfos: RaceLaneInfo[] = await startRace(
        task,
        models,
        workspacePath,
        handleLaneMessage,
        handleLaneError,
        handleLaneClosed
      );
      setLanes(
        laneInfos.map((l) => ({
          id: l.id,
          model: l.model,
          dir: l.dir,
          status: "running",
          tail: [],
          lastTool: null,
          finalResponse: null,
          error: null,
        }))
      );
      setPhase("racing");
    } catch (e) {
      setSetupError(`Failed to start race: ${e}`);
    } finally {
      setStarting(false);
    }
  }

  function handleLaneMessage({ laneId, event }: RaceLaneEvent) {
    setLanes((prev) =>
      prev.map((lane) => {
        if (lane.id !== laneId) return lane;

        switch (event.type) {
          case "token": {
            const buf = (streamBuffers.current[laneId] ?? "") + (event.content ?? "");
            streamBuffers.current[laneId] = buf;
            const lines = buf.split("\n").filter((l) => l.trim().length > 0);
            return { ...lane, tail: lines.slice(-MAX_TAIL_LINES) };
          }
          case "tool_start":
            return { ...lane, lastTool: event.name ?? lane.lastTool };
          case "chat_done":
            return {
              ...lane,
              status: "done",
              finalResponse: streamBuffers.current[laneId] ?? "",
            };
          case "error":
            return {
              ...lane,
              status: "failed",
              error: (event.content as string) ?? "Unknown error",
            };
          default:
            return lane;
        }
      })
    );
  }

  function handleLaneError(laneId: number, error: string) {
    setLanes((prev) =>
      prev.map((lane) => (lane.id === laneId ? { ...lane, error } : lane))
    );
  }

  function handleLaneClosed(laneId: number) {
    setLanes((prev) =>
      prev.map((lane) =>
        lane.id === laneId && lane.status === "running"
          ? { ...lane, status: "failed", error: "Lane process closed unexpectedly" }
          : lane
      )
    );
  }

  const allSettled = lanes.length > 0 && lanes.every((l) => l.status === "done" || l.status === "failed");

  // Auto-transition to compare phase when all lanes settle
  useEffect(() => {
    if (allSettled && !settledHandled.current) {
      settledHandled.current = true;
      setPhase("compare");
      // Kick off diff fetches for all done lanes
      for (const lane of lanes) {
        if (lane.status === "done") {
          setDiffs((prev) => ({ ...prev, [lane.id]: { diffText: null, diffLines: 0, loadingDiff: true, diffExpanded: false, merging: false, mergeMessage: null } }));
          fetchDiff(lane.id, lane.dir);
        } else {
          setDiffs((prev) => ({ ...prev, [lane.id]: { diffText: null, diffLines: 0, loadingDiff: false, diffExpanded: false, merging: false, mergeMessage: null } }));
        }
      }
    }
  }, [allSettled, lanes]);

  async function fetchDiff(laneId: number, laneDir: string) {
    try {
      const text = await diff_race_lane(workspacePath, laneDir);
      const lineCount = text.split("\n").filter((l) => l.trim().length > 0).length;
      setDiffs((prev) => ({
        ...prev,
        [laneId]: { ...prev[laneId], diffText: text, diffLines: lineCount, loadingDiff: false },
      }));
    } catch (e) {
      setDiffs((prev) => ({
        ...prev,
        [laneId]: { ...prev[laneId], diffText: null, diffLines: 0, loadingDiff: false, mergeMessage: `Failed to get diff: ${e}` },
      }));
    }
  }

  function toggleDiff(laneId: number) {
    setDiffs((prev) => ({
      ...prev,
      [laneId]: { ...prev[laneId], diffExpanded: !prev[laneId]?.diffExpanded },
    }));
  }

  async function handleMerge(laneId: number, laneDir: string) {
    setDiffs((prev) => ({
      ...prev,
      [laneId]: { ...prev[laneId], merging: true, mergeMessage: null },
    }));
    try {
      const result = await mergeRaceLane(workspacePath, laneDir);
      setDiffs((prev) => ({
        ...prev,
        [laneId]: { ...prev[laneId], merging: false, mergeMessage: `Merged — ${result.files_changed} file(s) changed.` },
      }));
      // Immediate merge then close, matching TUI behavior
      await killRace();
      onClose();
    } catch (e) {
      setDiffs((prev) => ({
        ...prev,
        [laneId]: { ...prev[laneId], merging: false, mergeMessage: `Merge failed: ${e}` },
      }));
    }
  }

  async function handleDiscard() {
    setDiscarding(true);
    try {
      await killRace();
    } catch {
      // Discard is best-effort; proceed to close regardless
    }
    onClose();
  }

  return (
    <div className="race-view">
      <div className="panel-header">
        <span>
          Race Mode
          {phase === "racing" ? ` — ${lanes.length} lanes` : ""}
          {phase === "compare" ? " — Compare & Merge" : ""}
        </span>
        {phase !== "compare" && <button onClick={onClose}>Back to chat</button>}
      </div>

      {phase === "setup" ? (
        <div className="race-setup">
          <label className="race-field">
            <span>Task</span>
            <textarea
              value={task}
              onChange={(e) => setTask(e.target.value)}
              placeholder="Describe what you want each model to attempt..."
              rows={4}
            />
          </label>
          <label className="race-field">
            <span>Models (comma-separated, 2-4)</span>
            <input
              value={modelsInput}
              onChange={(e) => setModelsInput(e.target.value)}
              placeholder="deepseek/deepseek-v4-flash, moonshotai/kimi-k2.7-code"
            />
          </label>
          {setupError && <div className="race-setup-error">{setupError}</div>}
          <button className="race-start-btn" onClick={handleStart} disabled={starting}>
            {starting ? "Starting race..." : "Start Race"}
          </button>
        </div>
      ) : phase === "racing" ? (
        <div className="race-lanes">
          {lanes.map((lane) => (
            <div key={lane.id} className={`race-lane race-lane-${lane.status}`}>
              <div className="race-lane-header">
                <span className="race-lane-model">{lane.model}</span>
                <span className={`race-lane-status-badge race-status-${lane.status}`}>
                  {lane.status === "pending" && "○"}
                  {lane.status === "running" && "●"}
                  {lane.status === "done" && "✓"}
                  {lane.status === "failed" && "✗"}
                  {" "}
                  {lane.status}
                </span>
              </div>
              {lane.lastTool && lane.status === "running" && (
                <div className="race-lane-tool">tool: {lane.lastTool}</div>
              )}
              {lane.status === "running" && (
                <pre className="race-lane-tail">{lane.tail.join("\n")}</pre>
              )}
              {lane.status === "done" && (
                <pre className="race-lane-final">{lane.finalResponse}</pre>
              )}
              {lane.status === "failed" && (
                <div className="race-lane-error">{lane.error}</div>
              )}
            </div>
          ))}
          {allSettled && (
            <div className="race-settled-note">All lanes settled. Loading diffs...</div>
          )}
        </div>
      ) : (
        /* ── Compare phase ─────────────────────────────────────── */
        <div className="race-compare">
          <div className="race-compare-intro">
            All lanes have finished. Review the changes and choose which lane to merge,
            or discard the race entirely.
          </div>

          {lanes.map((lane) => {
            const d = diffs[lane.id];
            const isDone = lane.status === "done";

            return (
              <div
                key={lane.id}
                className={`race-compare-lane ${isDone ? "race-compare-done" : "race-compare-failed"}`}
              >
                <div className="race-compare-lane-header">
                  <span className="race-lane-model">{lane.model}</span>
                  {isDone ? (
                    <span className="race-lane-status-badge race-status-done">✓ done</span>
                  ) : (
                    <span className="race-lane-status-badge race-status-failed">✗ failed</span>
                  )}
                </div>

                {isDone && d ? (
                  <div className="race-compare-diff-area">
                    {d.loadingDiff ? (
                      <div className="race-compare-diff-loading">Loading diff...</div>
                    ) : d.diffText !== null && d.diffText.length > 0 ? (
                      <>
                        <div className="race-compare-diff-summary">
                          <span className="race-compare-line-count">
                            {d.diffLines} line(s) changed
                          </span>
                          <button
                            className="race-compare-toggle-diff"
                            onClick={() => toggleDiff(lane.id)}
                          >
                            {d.diffExpanded ? "Hide Diff" : "View Diff"}
                          </button>
                        </div>
                        {d.diffExpanded && (
                          <pre className="race-compare-diff-text">
                            {d.diffText.split("\n").map((line, idx) => {
                              let cls = "diff-line-context";
                              if (line.startsWith("+") && !line.startsWith("+++")) cls = "diff-line-add";
                              else if (line.startsWith("-") && !line.startsWith("---")) cls = "diff-line-del";
                              else if (line.startsWith("@@")) cls = "diff-line-hunk";
                              else if (line.startsWith("+++") || line.startsWith("---")) cls = "diff-line-file";
                              return (
                                <div key={idx} className={cls}>
                                  {line || "\u00A0"}
                                </div>
                              );
                            })}
                          </pre>
                        )}
                      </>
                    ) : (
                      <div className="race-compare-diff-summary">
                        <span className="race-compare-no-changes">No differences</span>
                      </div>
                    )}

                    {d.mergeMessage && (
                      <div className={`race-compare-merge-msg ${d.mergeMessage.startsWith("Merge failed") ? "race-compare-merge-error" : "race-compare-merge-success"}`}>
                        {d.mergeMessage}
                      </div>
                    )}

                    <button
                      className="race-compare-merge-btn"
                      onClick={() => handleMerge(lane.id, lane.dir)}
                      disabled={d.merging || d.loadingDiff}
                    >
                      {d.merging ? "Merging..." : "Merge This Lane"}
                    </button>
                  </div>
                ) : !isDone ? (
                  <div className="race-compare-failed-note">
                    This lane failed to complete. No changes to merge.
                  </div>
                ) : null}
              </div>
            );
          })}

          <div className="race-compare-actions">
            <button
              className="race-compare-discard-btn"
              onClick={handleDiscard}
              disabled={discarding}
            >
              {discarding ? "Cleaning up..." : "Discard Race"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}