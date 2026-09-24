package tui

// False-finish regression suite.
//
// The reported failure: a multi-step coding turn returned to the prompt
// WITHOUT calling task_complete and only resumed after the user typed
// "finish" — and the TUI rendered that stop as a successful completion.
//
// Two halves are pinned here:
//
//  1. the engine no longer stops there (it auto-continues the same turn,
//     bounded) and the bridge relays the turn's OUTCOME on chat_done;
//  2. the TUI renders a truly terminal turn as success, and an
//     incomplete/control exit as an EXPLICIT incomplete/error state — never
//     as a successful completion.
//
// Frames are driven through the production engine-message path
// (handleEngineMsg) so the assertions exercise the real handlers.

import (
	"strings"
	"testing"

	"github.com/kp84-hub/kx/tui/components"
)

// timelineHasSuccessEvent reports whether any timeline event is a success.
func timelineHasSuccessEvent(m Model) bool {
	if m.Timeline == nil {
		return false
	}
	for _, ev := range m.Timeline.Events {
		if ev.Status == components.StatusSuccess {
			return true
		}
	}
	return false
}

// timelineEventByTitle returns the first timeline event whose title contains
// needle, or nil.
func timelineEventByTitle(m Model, needle string) *components.TimelineEvent {
	if m.Timeline == nil {
		return nil
	}
	for i := range m.Timeline.Events {
		if strings.Contains(m.Timeline.Events[i].Title, needle) {
			return &m.Timeline.Events[i]
		}
	}
	return nil
}

// TestIncompleteTurnNeverRendersAsSuccess pins the core fix: a chat_done that
// the bridge marked non-terminal ("incomplete") must NOT show the "Response
// complete" success event, must NOT fabricate an Overview, and must surface an
// explicit incomplete line in the transcript.
func TestIncompleteTurnNeverRendersAsSuccess(t *testing.T) {
	m := newConvModel()
	m.History = []string{"> fix the build and run the tests"}
	m.IsSending = true
	m.IsThinking = true
	m._timerActive = true
	m.CurrentTool = "edit_file"

	m = applyEngineFrames(m,
		MsgFromEngine{Type: "token", Content: "Now let me verify the change."},
		MsgFromEngine{
			Type:     "chat_done",
			Content:  "Now let me verify the change.",
			Outcome:  "incomplete",
			Terminal: false,
		},
		MsgFromEngine{Type: "phase", Value: "IDLE"},
	)

	// It must still return to idle on its own — no hang waiting for "finish".
	if m.Phase != PhaseIdle || m.IsSending || m.IsThinking || m._timerActive {
		t.Fatalf("incomplete turn did not return to idle: phase=%q sending=%v thinking=%v timer=%v",
			m.Phase, m.IsSending, m.IsThinking, m._timerActive)
	}

	// Never a success event, never a fabricated Overview.
	if timelineHasSuccessEvent(m) {
		t.Fatalf("incomplete turn rendered a success timeline event: %#v", m.Timeline.Events)
	}
	if ev := timelineEventByTitle(m, "Response complete"); ev != nil {
		t.Fatalf("incomplete turn rendered %q", ev.Title)
	}
	for _, h := range m.History {
		if strings.HasPrefix(h, "_Overview:_") {
			t.Fatalf("incomplete turn fabricated an Overview: %#v", m.History)
		}
	}

	// An explicit incomplete line is surfaced instead.
	joined := strings.Join(m.History, "\n")
	if !strings.Contains(joined, "Turn incomplete") {
		t.Fatalf("incomplete turn did not surface an explicit incomplete state:\n%s", joined)
	}
	rendered := stripANSI(m.FullViewportContent(100))
	if !strings.Contains(rendered, "Turn incomplete") {
		t.Fatalf("incomplete state not rendered:\n%s", rendered)
	}
}

// TestControlExitRendersExplicitErrorNotSuccess pins that every control/abort
// exit (loop, circuit breaker, max recursion, provider error) is surfaced as a
// failure — never as a successful completion.
func TestControlExitRendersExplicitErrorNotSuccess(t *testing.T) {
	cases := []struct {
		outcome string
		label   string
	}{
		{"loop", "loop detected"},
		{"circuit_breaker", "circuit breaker"},
		{"max_recursion", "maximum reasoning depth"},
		{"provider_error", "provider error"},
		{"error", "engine error"},
	}
	for _, tc := range cases {
		m := newConvModel()
		m.History = []string{"> do the thing"}
		m.IsSending = true

		m = applyEngineFrames(m,
			MsgFromEngine{Type: "chat_done", Content: "stopped", Outcome: tc.outcome},
			MsgFromEngine{Type: "phase", Value: "IDLE"},
		)

		if timelineHasSuccessEvent(m) {
			t.Fatalf("outcome %q rendered a success event: %#v", tc.outcome, m.Timeline.Events)
		}
		ev := timelineEventByTitle(m, tc.label)
		if ev == nil {
			t.Fatalf("outcome %q did not render an explicit %q event: %#v",
				tc.outcome, tc.label, m.Timeline.Events)
		}
		if ev.Status != components.StatusFailed {
			t.Fatalf("outcome %q event status = %q, want failed", tc.outcome, ev.Status)
		}
		if !strings.Contains(strings.Join(m.History, "\n"), "Turn aborted") {
			t.Fatalf("outcome %q did not surface an explicit abort line: %#v",
				tc.outcome, m.History)
		}
	}
}

// TestTerminalTurnStillRendersSuccess proves the boundary did not overreach: a
// truly terminal turn (explicit task_complete) still renders the success event
// and its Overview.
func TestTerminalTurnStillRendersSuccess(t *testing.T) {
	m := newConvModel()
	m.History = []string{"> fix the build"}
	m.IsSending = true

	m = applyEngineFrames(m,
		MsgFromEngine{Type: "token", Content: "The build is fixed."},
		MsgFromEngine{
			Type:     "chat_done",
			Content:  "The build is fixed.\n[Task Complete: build green]",
			Outcome:  "complete",
			Terminal: true,
		},
		MsgFromEngine{Type: "phase", Value: "IDLE"},
	)

	if ev := timelineEventByTitle(m, "Response complete"); ev == nil {
		t.Fatalf("terminal turn lost its success event: %#v", m.Timeline.Events)
	} else if ev.Status != components.StatusSuccess {
		t.Fatalf("terminal turn success event status = %q", ev.Status)
	}
	joined := strings.Join(m.History, "\n")
	if !strings.Contains(joined, "_Overview:_") || !strings.Contains(joined, "build green") {
		t.Fatalf("terminal turn lost its Overview: %#v", m.History)
	}
	if strings.Contains(joined, "Turn incomplete") || strings.Contains(joined, "Turn aborted") {
		t.Fatalf("terminal turn wrongly surfaced an incomplete state: %#v", m.History)
	}
}

// TestEmptyOutcomeStaysTerminal pins backward compatibility: a chat_done with
// no outcome (older emitters, other surfaces) still renders as success.
func TestEmptyOutcomeStaysTerminal(t *testing.T) {
	m := newConvModel()
	m.History = []string{"> hello"}
	m = applyEngineFrames(m,
		MsgFromEngine{Type: "token", Content: "Hi there."},
		MsgFromEngine{Type: "chat_done", Content: "Hi there."},
		MsgFromEngine{Type: "phase", Value: "IDLE"},
	)

	if ev := timelineEventByTitle(m, "Response complete"); ev == nil {
		t.Fatalf("empty outcome lost the success event: %#v", m.Timeline.Events)
	}
	if !strings.Contains(strings.Join(m.History, "\n"), "Hi there.") {
		t.Fatalf("empty outcome lost transcript content: %#v", m.History)
	}
}

// TestAutoContinuedTurnKeepsOneTranscriptAndIdleState proves an
// auto-continued turn (several engine rounds, ONE chat_done) renders exactly
// like any other completed turn: the streamed rounds are preserved, the live
// buffers are flushed, and the turn returns to idle.
func TestAutoContinuedTurnKeepsOneTranscriptAndIdleState(t *testing.T) {
	m := newConvModel()
	m.History = []string{"> do the multi-step thing"}
	m.IsSending = true

	// Round 1 streamed, then the engine auto-continued; round 2 streamed and
	// completed. The TUI sees the usual frames — only ONE chat_done at the end.
	m = applyEngineFrames(m,
		MsgFromEngine{Type: "token", Content: "Step one done."},
		MsgFromEngine{Type: "token", Content: roundSeparator},
		MsgFromEngine{Type: "token", Content: "Step two done."},
		MsgFromEngine{
			Type:     "chat_done",
			Content:  "Step one done.\nStep two done.\n[Task Complete: all steps]",
			Outcome:  "complete",
			Terminal: true,
		},
		MsgFromEngine{Type: "phase", Value: "IDLE"},
	)

	joined := strings.Join(m.History, "\n")
	if !strings.Contains(joined, "Step one done.") || !strings.Contains(joined, "Step two done.") {
		t.Fatalf("auto-continued rounds lost from the transcript:\n%s", joined)
	}
	if m.CurrToken != "" || m.Reasoning != "" {
		t.Fatalf("live buffers not flushed: curr=%q reasoning=%q", m.CurrToken, m.Reasoning)
	}
	if m.Phase != PhaseIdle || m.IsSending {
		t.Fatalf("auto-continued turn not idle: phase=%q sending=%v", m.Phase, m.IsSending)
	}
}
