package tui

// Native-completion regression suite.
//
// These tests pin the TUI contract for a turn the engine completes via its
// native fallback — the model delivered its final answer as ordinary content
// and never called task_complete, so core.py ended the turn after two
// consecutive meaningful tool-less rounds. The bridge then emits exactly one
// chat_done followed by a phase:IDLE sync. The TUI must:
//   - return to idle with no further user message (sending/timer/thinking off,
//     current tool state cleared)
//   - preserve the transcript content it already streamed
//   - NOT invent an Overview or show a false "assumed complete" success marker
//   - settle any pending approval gate exactly as an explicit-task_complete
//     turn would (deny, never strand)
//
// Frames are driven through the production engine-message path
// (handleEngineMsg) so the assertions exercise the real handlers, not a
// test-only shortcut.

import (
	"strings"
	"testing"
)

// applyEngineFrames drives engine frames through the real production path.
func applyEngineFrames(m Model, events ...MsgFromEngine) Model {
	for _, ev := range events {
		m, _, _ = m.handleEngineMsg(ev)
	}
	return m
}

// TestNativeCompletionReturnsTUIIdleWithoutUserMessage proves a turn that ends
// via the engine's native fallback leaves the TUI idle on its own: no second
// user message is required, and every transient per-turn signal is cleared
// while the streamed transcript is preserved.
func TestNativeCompletionReturnsTUIIdleWithoutUserMessage(t *testing.T) {
	m := newConvModel()
	m.History = []string{"> fix the build"}
	// A live turn: sending animation, turn timer and thinking all engaged, a
	// tool was the last activity.
	m.IsSending = true
	m.IsThinking = true
	m._timerActive = true
	m.Timer = 7
	m._sendingTick = 2
	m.CurrentTool = "read_local_file"
	m.ToolArgs = "tui/model.go"
	m.ToolResult = "OK"

	m = applyEngineFrames(m,
		MsgFromEngine{Type: "token", Content: "First round: the build is fixed."},
		MsgFromEngine{Type: "token", Content: roundSeparator},
		MsgFromEngine{Type: "token", Content: "Second round: all tests pass."},
		// The bridge's completion frames: one chat_done + an IDLE phase sync.
		MsgFromEngine{Type: "chat_done", Content: "First round: the build is fixed.\nSecond round: all tests pass."},
		MsgFromEngine{Type: "phase", Value: "IDLE"},
	)

	if m.Phase != PhaseIdle {
		t.Fatalf("phase = %q, want IDLE", m.Phase)
	}
	if m.IsSending || m.IsThinking || m._timerActive {
		t.Fatalf("turn still running after chat_done: IsSending=%v IsThinking=%v timerActive=%v",
			m.IsSending, m.IsThinking, m._timerActive)
	}
	if m._sendingTick != 0 {
		t.Fatalf("sending animation frame not reset: %d", m._sendingTick)
	}
	if m.CurrentTool != "" || m.ToolArgs != "" || m.ToolResult != "" {
		t.Fatalf("stale current tool state: tool=%q args=%q result=%q", m.CurrentTool, m.ToolArgs, m.ToolResult)
	}
	if m.CurrToken != "" || m.Reasoning != "" {
		t.Fatalf("live stream buffers not flushed: curr=%q reasoning=%q", m.CurrToken, m.Reasoning)
	}

	// Transcript content survived.
	joined := strings.Join(m.History, "\n")
	if !strings.Contains(joined, "First round: the build is fixed.") {
		t.Fatalf("first round content lost:\n%s", joined)
	}
	if !strings.Contains(joined, "Second round: all tests pass.") {
		t.Fatalf("second round content lost:\n%s", joined)
	}
}

// TestNativeCompletionShowsNoFalseSuccessMarker proves the fallback does not
// fabricate an Overview or show any "assumed complete" success marker.
func TestNativeCompletionShowsNoFalseSuccessMarker(t *testing.T) {
	m := newConvModel()
	m.History = []string{"> summarize"}
	m = applyEngineFrames(m,
		MsgFromEngine{Type: "token", Content: "Everything is in order."},
		MsgFromEngine{Type: "token", Content: roundSeparator},
		MsgFromEngine{Type: "token", Content: "No further action is needed."},
		MsgFromEngine{Type: "chat_done", Content: "Everything is in order.\nNo further action is needed."},
	)

	for _, h := range m.History {
		if strings.HasPrefix(h, "_Overview:_") {
			t.Fatalf("fallback fabricated an Overview: %#v", m.History)
		}
	}
	rendered := stripANSI(m.FullViewportContent(100))
	if strings.Contains(rendered, "Overview") {
		t.Fatalf("false success Overview rendered:\n%s", rendered)
	}
	if strings.Contains(rendered, "[Task assumed complete") || strings.Contains(rendered, "[Task Complete:") {
		t.Fatalf("false success marker rendered:\n%s", rendered)
	}
}

// TestNativeCompletionSettlesPendingApproval proves the chat_done emitted for
// a native-completion turn settles an engine-backed approval gate exactly like
// an explicit task_complete turn: the blocked operation is denied (never
// stranded) and no pending approval remains.
func TestNativeCompletionSettlesPendingApproval(t *testing.T) {
	f := newApprovalFakeEngine()
	m := newApprovalTestModel(f)
	g := f.registerGate("g1")
	m = injectConfirm(t, m, "g1", "/tmp/a.txt")
	m.IsSending = true
	m._timerActive = true

	m = applyEngineFrames(m,
		MsgFromEngine{Type: "chat_done", Content: "done", Reasoning: ""},
		MsgFromEngine{Type: "phase", Value: "IDLE"},
	)

	awaitGateDecision(t, g, false) // denied, not stranded
	assertNoPendingApproval(t, m)
	if m.IsSending || m._timerActive || m.IsThinking {
		t.Fatalf("TUI not idle after native completion: IsSending=%v timerActive=%v IsThinking=%v",
			m.IsSending, m._timerActive, m.IsThinking)
	}
}

// ── Turn boundary: late frames cannot resurrect a completed turn ───────

// TestLateFramesAfterChatDoneDoNotResurrectTurn pins the turn boundary: once
// chat_done has finalized a turn, a late frame belonging to that turn must not
// re-open it — no thinking / sending / timer / current-tool state, no live
// stream content, no diff pane, and no phase change.
func TestLateFramesAfterChatDoneDoNotResurrectTurn(t *testing.T) {
	m := newConvModel()
	m.History = []string{"> fix the build"}
	m.IsSending = true
	m.IsThinking = true
	m._timerActive = true
	m.CurrentTool = "read_local_file"
	m.ToolArgs = "tui/model.go"
	m.ToolResult = "OK"

	// The completed turn: one round, its completion, the terminal IDLE sync.
	m = applyEngineFrames(m,
		MsgFromEngine{Type: "token", Content: "The build is fixed."},
		MsgFromEngine{Type: "chat_done", Content: "The build is fixed."},
		MsgFromEngine{Type: "phase", Value: "IDLE"},
	)

	if !m._turnComplete {
		t.Fatal("chat_done did not set the turn-complete boundary")
	}
	if m.Phase != PhaseIdle {
		t.Fatalf("phase = %q, want IDLE", m.Phase)
	}

	// Late frames belonging to the completed turn.
	m = applyEngineFrames(m,
		MsgFromEngine{Type: "token", Content: "late token"},
		MsgFromEngine{Type: "content", Content: "late content"},
		MsgFromEngine{Type: "reasoning", Content: "late reasoning"},
		MsgFromEngine{Type: "tool_start", Name: "edit_file", Args: map[string]interface{}{"path": "x.go"}},
		MsgFromEngine{Type: "tool_result", ID: "late-tool", Result: map[string]interface{}{"status": "ok"}},
		MsgFromEngine{Type: "diff", ID: "late-diff", Path: "x.go", Diff: "--- a/x.go\n+++ b/x.go\n+late"},
		MsgFromEngine{Type: "log", Content: "late log line"},
		MsgFromEngine{Type: "phase", Value: "EXECUTE"},
	)

	if m.IsThinking || m.IsSending || m._timerActive {
		t.Fatalf("late frames resurrected turn state: IsThinking=%v IsSending=%v timerActive=%v",
			m.IsThinking, m.IsSending, m._timerActive)
	}
	if m.CurrentTool != "" || m.ToolArgs != "" || m.ToolResult != "" {
		t.Fatalf("late tool frames restored current-tool state: tool=%q args=%q result=%q",
			m.CurrentTool, m.ToolArgs, m.ToolResult)
	}
	if m.CurrToken != "" || m.Reasoning != "" {
		t.Fatalf("late stream frames accumulated live content: curr=%q reasoning=%q", m.CurrToken, m.Reasoning)
	}
	if len(m.DiffBlocks) != 0 || m.ActiveDiffID != "" {
		t.Fatalf("late diff frame was applied: blocks=%#v active=%q", m.DiffBlocks, m.ActiveDiffID)
	}
	if m.Phase != PhaseIdle {
		t.Fatalf("late phase frame changed phase to %q", m.Phase)
	}
	if !m._turnComplete {
		t.Fatal("late frames cleared the turn-complete boundary")
	}
}

// TestLateFramesAfterChatDoneDoNotChangeTranscript pins the other half of the
// boundary: the completed transcript is frozen — late frames append nothing.
func TestLateFramesAfterChatDoneDoNotChangeTranscript(t *testing.T) {
	m := newConvModel()
	m.History = []string{"> check the build"}
	m = applyEngineFrames(m,
		MsgFromEngine{Type: "token", Content: "First round committed."},
		MsgFromEngine{Type: "token", Content: roundSeparator},
		MsgFromEngine{Type: "token", Content: "Second round committed."},
		MsgFromEngine{Type: "chat_done", Content: "First round committed.\nSecond round committed."},
		MsgFromEngine{Type: "phase", Value: "IDLE"},
	)

	before := strings.Join(m.History, "\n")

	m = applyEngineFrames(m,
		MsgFromEngine{Type: "token", Content: "late and unwanted"},
		MsgFromEngine{Type: "reasoning", Content: "late reasoning"},
		MsgFromEngine{Type: "tool_start", Name: "edit_file", Args: map[string]interface{}{"path": "x.go"}},
		MsgFromEngine{Type: "tool_result", ID: "late-tool", Result: map[string]interface{}{"status": "ok"}},
		MsgFromEngine{Type: "log", Content: "late log"},
		MsgFromEngine{Type: "confirm_request", ID: "late-confirm", Path: "/tmp/x", Diff: "+x"},
	)

	if got := strings.Join(m.History, "\n"); got != before {
		t.Fatalf("late frames mutated the transcript:\n--- before ---\n%s\n--- after ---\n%s", before, got)
	}
	rendered := stripANSI(m.FullViewportContent(100))
	for _, leaked := range []string{"late and unwanted", "late reasoning", "late log"} {
		if strings.Contains(rendered, leaked) {
			t.Fatalf("late frame content rendered into the transcript: %q\n%s", leaked, rendered)
		}
	}
}

// TestNextTurnFramesAcceptedAfterChatDone proves the boundary is per-turn: once
// the next turn begins, engine frames (including a non-IDLE phase) flow again.
func TestNextTurnFramesAcceptedAfterChatDone(t *testing.T) {
	m := newConvModel()
	m.History = []string{"> first"}
	m = applyEngineFrames(m,
		MsgFromEngine{Type: "token", Content: "first answer"},
		MsgFromEngine{Type: "chat_done", Content: "first answer"},
		MsgFromEngine{Type: "phase", Value: "IDLE"},
	)

	// A late token is rejected.
	m = applyEngineFrames(m, MsgFromEngine{Type: "token", Content: "late"})
	if m.CurrToken != "" {
		t.Fatalf("late token accepted after completion: %q", m.CurrToken)
	}

	// The next turn begins on submit.
	m.resetTurnState()
	if m._turnComplete {
		t.Fatal("resetTurnState did not reopen the turn boundary")
	}
	m = applyEngineFrames(m,
		MsgFromEngine{Type: "token", Content: "second answer"},
		MsgFromEngine{Type: "phase", Value: "EXECUTE"},
	)
	if m.CurrToken != "second answer" {
		t.Fatalf("next-turn token rejected: %q", m.CurrToken)
	}
	if m.Phase != PhaseExecute {
		t.Fatalf("next-turn phase rejected: %q", m.Phase)
	}
}

// TestStartupAndSessionFramesAcceptedBeforeAnyTurn proves the boundary does not
// gate pre-turn startup frames: session_state, the initial IDLE phase and
// silent usage pauses reach the TUI before any chat_done has occurred.
func TestStartupAndSessionFramesAcceptedBeforeAnyTurn(t *testing.T) {
	m := newConvModel()
	m = applyEngineFrames(m,
		MsgFromEngine{Type: "session_state", Model: "test-model", Provider: "stub", Context: "ws"},
		MsgFromEngine{Type: "phase", Value: "IDLE"},
		MsgFromEngine{Type: "tui_pause", Value: "usage_stats_silent", Files: map[string]interface{}{"prompt_tokens": float64(1)}},
	)
	if m._turnComplete {
		t.Fatal("startup frames wrongly set the turn-complete boundary")
	}
	if m.Phase != PhaseIdle {
		t.Fatalf("startup IDLE phase not applied: %q", m.Phase)
	}
	if !strings.Contains(m.LLMInfo, "test-model") {
		t.Fatalf("session_state not applied: %q", m.LLMInfo)
	}
}

// TestLateFramesAfterChatDoneDoNotResettleApproval proves the pending Gate A
// approval is settled exactly once: the completion that follows chat_done
// cannot re-open or re-settle it, and the engine receives exactly one
// confirm_response.
func TestLateFramesAfterChatDoneDoNotResettleApproval(t *testing.T) {
	f := newApprovalFakeEngine()
	m := newApprovalTestModel(f)
	g1 := f.registerGate("g1")
	m = injectConfirm(t, m, "g1", "/tmp/a.txt")

	m = applyEngineFrames(m,
		MsgFromEngine{Type: "chat_done", Content: "done", Reasoning: ""},
		MsgFromEngine{Type: "phase", Value: "IDLE"},
	)
	awaitGateDecision(t, g1, false) // settled exactly once, as a denial
	assertNoPendingApproval(t, m)

	// A late confirm_request must not re-open a gate; a late chat_done must not
	// re-settle anything.
	g2 := f.registerGate("g2")
	m = applyEngineFrames(m,
		MsgFromEngine{Type: "confirm_request", ID: "g2", Path: "/tmp/b.txt", Diff: "+x"},
		MsgFromEngine{Type: "chat_done", Content: "late done"},
	)

	if m.ConfirmID != "" {
		t.Fatalf("late confirm_request re-opened a gate: %q", m.ConfirmID)
	}
	if got := f.countType("confirm_response"); got != 1 {
		t.Fatalf("approval settled %d times, want exactly 1", got)
	}
	assertNoDecision(t, g2)
	if !m._turnComplete {
		t.Fatal("late chat_done cleared the turn boundary")
	}
}
