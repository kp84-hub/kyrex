package tui

import (
	"fmt"
	"strings"
	"testing"

	"github.com/kp84-hub/kx/tui/components"
)

// applyEvents drives engine events through the real production path
// (handleEngineMsg) so the round-segmentation logic is exercised end to end.
func applyEvents(m Model, events ...MsgFromEngine) Model {
	for _, ev := range events {
		m, _, _ = m.handleEngineMsg(ev)
	}
	return m
}

// newConvModel returns a model with a deterministic (verbose) verbosity and a
// usable viewport width for rendering assertions.
func newConvModel() Model {
	m := NewModel(nil)
	m.Verbosity = VerbosityVerbose
	m.Viewport.Width = 100
	return m
}

// TestKYREXThoughtKYREXOrdering asserts the core conversation hierarchy:
// each provider round commits as KYREX content followed by a compact Thought,
// in the exact order the engine's events arrived, with Overview last.
func TestKYREXThoughtKYREXOrdering(t *testing.T) {
	m := newConvModel()
	m.History = []string{"> check the session handling"}
	m = applyEvents(m,
		MsgFromEngine{Type: "reasoning", Content: "The request reaches the provider, but the session header is missing before the request is sent."},
		MsgFromEngine{Type: "token", Content: "I found the issue in the session handling."},
		MsgFromEngine{Type: "token", Content: roundSeparator},
		MsgFromEngine{Type: "reasoning", Content: "The provider config already knows the gateway; the missing piece is session initialization."},
		MsgFromEngine{Type: "token", Content: "I'm tracing where that header gets constructed."},
		// Engine's real payload is the whole-turn concatenation plus the
		// model's own task_complete summary. The TUI must not duplicate
		// already-committed rounds from it.
		MsgFromEngine{Type: "chat_done", Content: "I found the issue in the session handling.\nI'm tracing where that header gets constructed.\n[Task Complete: Fixed session init. Tests pass.]"},
	)

	want := []string{
		"> check the session handling",
		"_Assistant:_\nI found the issue in the session handling.",
		"_Thought:_\nThe request reaches the provider, but the session header is missing before the request is sent.",
		"_Assistant:_\nI'm tracing where that header gets constructed.",
		"_Thought:_\nThe provider config already knows the gateway; the missing piece is session initialization.",
		"_Overview:_\nFixed session init. Tests pass.",
	}
	if len(m.History) != len(want) {
		t.Fatalf("history len = %d, want %d:\n%#v", len(m.History), len(want), m.History)
	}
	for i, w := range want {
		if m.History[i] != w {
			t.Errorf("history[%d] = %q\n       want %q", i, m.History[i], w)
		}
	}

	// Rendered order: user → KYREX → Thought → KYREX → Thought → Overview.
	rendered, _ := m.HistoryContent(100)
	clean := stripANSI(rendered)
	k1 := strings.Index(clean, "I found the issue in the session handling.")
	t1 := strings.Index(clean, "session header is missing before")
	k2 := strings.Index(clean, "I'm tracing where that header")
	t2 := strings.Index(clean, "provider config already knows")
	ov := strings.Index(clean, "Overview")
	if !(k1 >= 0 && k1 < t1 && t1 < k2 && k2 < t2 && t2 < ov) {
		t.Fatalf("render order wrong: k1=%d t1=%d k2=%d t2=%d ov=%d\n%s", k1, t1, k2, t2, ov, clean)
	}
}

// TestMultipleAlternatingEvents asserts several tool-bounded rounds keep their
// KYREX/Thought pairs distinct and in order.
func TestMultipleAlternatingEvents(t *testing.T) {
	m := newConvModel()
	m.History = []string{"> big task"}
	m = applyEvents(m,
		MsgFromEngine{Type: "reasoning", Content: "t1"},
		MsgFromEngine{Type: "token", Content: "k1 message"},
		MsgFromEngine{Type: "token", Content: roundSeparator},
		MsgFromEngine{Type: "tool_start", Name: "read_local_file", Args: map[string]interface{}{"path": "a.go"}},
		MsgFromEngine{Type: "tool_result", Name: "read_local_file", Result: map[string]interface{}{}},
		MsgFromEngine{Type: "reasoning", Content: "t2"},
		MsgFromEngine{Type: "token", Content: "k2 message"},
		MsgFromEngine{Type: "token", Content: roundSeparator},
		MsgFromEngine{Type: "tool_start", Name: "edit_file", Args: map[string]interface{}{"path": "a.go"}},
		MsgFromEngine{Type: "tool_result", Name: "edit_file", Result: map[string]interface{}{}},
		MsgFromEngine{Type: "reasoning", Content: "t3"},
		MsgFromEngine{Type: "token", Content: "k3 message"},
		MsgFromEngine{Type: "chat_done", Content: "k1 message\nk2 message\nk3 message\n[Task Complete: All done.]"},
	)

	want := []string{
		"> big task",
		"_Assistant:_\nk1 message",
		"_Thought:_\nt1",
		"_Assistant:_\nk2 message",
		"_Thought:_\nt2",
		"_Assistant:_\nk3 message",
		"_Thought:_\nt3",
		"_Overview:_\nAll done.",
	}
	if len(m.History) != len(want) {
		t.Fatalf("history len = %d, want %d:\n%#v", len(m.History), len(want), m.History)
	}
	for i, w := range want {
		if m.History[i] != w {
			t.Errorf("history[%d] = %q\n       want %q", i, m.History[i], w)
		}
	}
}

// TestStreamingUpdatesExistingMessage asserts token frames never create
// token-sized blocks: the live KYREX message grows in place, and a round
// boundary commits exactly one history entry for the whole message.
func TestStreamingUpdatesExistingMessage(t *testing.T) {
	m := newConvModel()
	m.History = []string{"> check config"}

	for _, frame := range []string{"I'm", " checking", " the provider", " config..."} {
		m, _, _ = m.handleEngineMsg(MsgFromEngine{Type: "token", Content: frame})
		if len(m.History) != 1 {
			t.Fatalf("streaming created history entries: %#v", m.History)
		}
	}

	// After the burst: exactly one live KYREX block holding the whole message.
	clean := stripANSI(m.FullViewportContent(100))
	if n := strings.Count(clean, "KYREX"); n != 1 {
		t.Fatalf("expected exactly one live KYREX block, got %d:\n%s", n, clean)
	}
	if !strings.Contains(clean, "I'm checking the provider config...") {
		t.Fatalf("live block must grow in place; missing accumulated text:\n%s", clean)
	}

	// Boundary commits the single rounded message once, not per token.
	m, _, _ = m.handleEngineMsg(MsgFromEngine{Type: "token", Content: roundSeparator})
	if len(m.History) != 2 {
		t.Fatalf("expected exactly one committed round, got:\n%#v", m.History)
	}
	if got := m.History[1]; got != "_Assistant:_\nI'm checking the provider config..." {
		t.Fatalf("committed round mismatch: %q", got)
	}
	historyRendered, _ := m.HistoryContent(100)
	cleanScreen := stripANSI(historyRendered)
	if n := strings.Count(cleanScreen, "I'm checking the provider config..."); n != 1 {
		t.Fatalf("accumulated text duplicated across blocks: %d\n%s", n, cleanScreen)
	}
}

// TestLongThoughtRemainsBounded asserts a very long reasoning event stays
// compact at render time (with a truncation marker) while the full text is
// still stored in History — nothing is fabricated or lost.
func TestLongThoughtRemainsBounded(t *testing.T) {
	m := newConvModel()
	m.History = []string{"> task"}
	var long strings.Builder
	for i := 1; i <= 40; i++ {
		fmt.Fprintf(&long, "step %d: puny reasoning detail line with some length to wrap\n", i)
	}
	m = applyEvents(m,
		MsgFromEngine{Type: "reasoning", Content: long.String()},
		MsgFromEngine{Type: "token", Content: "I did it."},
		MsgFromEngine{Type: "chat_done", Content: "I did it.\n[Task Complete: Done.]"},
	)

	// Full reasoning text still stored (round commits as Assistant then Thought).
	if !strings.Contains(m.History[2], "step 40:") {
		t.Fatalf("full reasoning lost: %#v", m.History)
	}

	rendered, _ := m.HistoryContent(100)
	clean := stripANSI(rendered)
	if !strings.Contains(clean, "+34 lines") {
		t.Fatalf("expected truncation marker, got:\n%s", clean)
	}
	// Thought block must stay compact: label + <= maxThoughtLines body +
	// marker + blanks. Deep reasoning detail must not leak through.
	lines := strings.Split(clean, "\n")
	thoughtIdx, ovIdx := -1, -1
	for i, ln := range lines {
		if ln == "Thought" && thoughtIdx == -1 {
			thoughtIdx = i
		}
		// The Overview header renders with a leading icon glyph.
		if strings.Contains(ln, "Overview") && ovIdx == -1 {
			ovIdx = i
		}
	}
	if thoughtIdx == -1 || ovIdx == -1 || ovIdx-thoughtIdx > 12 {
		t.Fatalf("thought block not bounded (thoughtIdx=%d ovIdx=%d):\n%s", thoughtIdx, ovIdx, clean)
	}
	if strings.Contains(clean, "step 40:") {
		t.Fatalf("truncated reasoning leaked into render:\n%s", clean)
	}
}

// TestToolEventsRemainSeparate asserts tool activity never becomes KYREX or
// Thought content: it stays in the execution timeline/telemetry, and the
// whole-turn fallback does not duplicate already-committed rounds.
func TestToolEventsRemainSeparate(t *testing.T) {
	m := newConvModel()
	m.History = []string{"> task"}
	m = applyEvents(m,
		MsgFromEngine{Type: "reasoning", Content: "need to read the file"},
		MsgFromEngine{Type: "token", Content: "Let me check the file."},
		MsgFromEngine{Type: "token", Content: roundSeparator},
		MsgFromEngine{Type: "tool_start", Name: "read_local_file", Args: map[string]interface{}{"path": "tui/model.go"}},
		MsgFromEngine{Type: "tool_result", Name: "read_local_file", Result: map[string]interface{}{}},
		MsgFromEngine{Type: "chat_done", Content: "Let me check the file.\n[Task Complete: Verified.]"},
	)

	// No tool names inside conversation entries; no duplicated assistant block.
	for i, h := range m.History {
		if strings.Contains(h, "read_local_file") {
			t.Errorf("tool leaked into transcript entry %d: %q", i, h)
		}
	}
	// Round 1 committed as (Assistant, Thought); the whole-turn fallback is
	// skipped (rounds already committed) so nothing is duplicated; Overview
	// ends the turn.
	want := []string{
		"> task",
		"_Assistant:_\nLet me check the file.",
		"_Thought:_\nneed to read the file",
		"_Overview:_\nVerified.",
	}
	if len(m.History) != len(want) {
		t.Fatalf("history len = %d, want %d:\n%#v", len(m.History), len(want), m.History)
	}
	for i, w := range want {
		if m.History[i] != w {
			t.Errorf("history[%d] = %q\n       want %q", i, m.History[i], w)
		}
	}

	// Tool telemetry/timeline still carry the tool, separately.
	toolTimeline := 0
	for _, e := range m.Timeline.Events {
		if e.Type == components.EventTool {
			toolTimeline++
		}
	}
	if toolTimeline != 1 {
		t.Fatalf("expected 1 tool timeline event, got %d", toolTimeline)
	}
	if tools := m.Tools.Recent(); len(tools) != 1 || tools[0].State != ToolStateSuccess {
		t.Fatalf("unexpected tool telemetry: %#v", m.Tools.Recent())
	}
}

// TestFinalOverviewAppearsAtEnd asserts the concise engine-derived Overview is
// the last history entry and is rendered at the end of the transcript.
func TestFinalOverviewAppearsAtEnd(t *testing.T) {
	m := newConvModel()
	m.History = []string{"> task"}
	m = applyEvents(m,
		MsgFromEngine{Type: "token", Content: "Work finished."},
		MsgFromEngine{Type: "chat_done", Content: "Work finished.\n[Task Complete: Fixed session init.\nEach setup now generates a fresh session ID automatically.\nFocused tests pass.]"},
	)

	if len(m.History) != 3 {
		t.Fatalf("history: %#v", m.History)
	}
	last := m.History[2]
	if !strings.HasPrefix(last, "_Overview:_\n") {
		t.Fatalf("expected Overview last, got %q", last)
	}
	summary := strings.TrimPrefix(last, "_Overview:_\n")
	if summary != "Fixed session init.\nEach setup now generates a fresh session ID automatically.\nFocused tests pass." {
		t.Fatalf("unexpected summary: %q", summary)
	}

	rendered, _ := m.HistoryContent(100)
	clean := strings.TrimSpace(stripANSI(rendered))
	if !strings.HasSuffix(clean, "Focused tests pass.") {
		t.Fatalf("Overview not rendered at end:\n%s", clean)
	}
	if strings.Contains(clean, "[Task Complete:") {
		t.Fatalf("raw task_complete marker leaked into render:\n%s", clean)
	}
}

// TestKYREXOnlyResponse asserts a plain single-round answer produces exactly
// one KYREX message and no fabricated Thought or Overview.
func TestKYREXOnlyResponse(t *testing.T) {
	m := newConvModel()
	m.History = []string{"> question?"}
	m = applyEvents(m,
		MsgFromEngine{Type: "token", Content: "Just an answer."},
		MsgFromEngine{Type: "chat_done", Content: "Just an answer."},
	)
	if len(m.History) != 2 {
		t.Fatalf("history: %#v", m.History)
	}
	if m.History[1] != "_Assistant:_\nJust an answer." {
		t.Fatalf("history[1] = %q", m.History[1])
	}

	rendered, _ := m.HistoryContent(100)
	clean := stripANSI(rendered)
	if strings.Contains(clean, "Thought") || strings.Contains(clean, "Overview") {
		t.Fatalf("thought/overview must not appear for KYREX-only response:\n%s", clean)
	}
}

// TestThoughtOnlyEvent asserts a reasoning-only round surfaces as a Thought
// block alone — never as a phantom KYREX message or Overview.
func TestThoughtOnlyEvent(t *testing.T) {
	m := newConvModel()
	m.History = []string{"> task"}
	m = applyEvents(m,
		MsgFromEngine{Type: "reasoning", Content: "deep thought"},
		MsgFromEngine{Type: "chat_done", Content: "", Reasoning: "deep thought"},
	)
	if len(m.History) != 2 {
		t.Fatalf("history: %#v", m.History)
	}
	if m.History[1] != "_Thought:_\ndeep thought" {
		t.Fatalf("history[1] = %q", m.History[1])
	}
}

// TestErrorAndInterruptEvents asserts error frames surface as ERROR entries +
// timeline events, interrupted turns don't fabricate new blocks, and a
// ChatDone placeholder is never surfaced as assistant speech.
func TestErrorAndInterruptEvents(t *testing.T) {
	m := newConvModel()
	m.History = []string{"> task"}

	// Error mid-stream.
	m = applyEvents(m,
		MsgFromEngine{Type: "reasoning", Content: "partial thought"},
		MsgFromEngine{Type: "token", Content: "partial answer"},
		MsgFromEngine{Type: "error", Content: "Connection reset by provider"},
	)
	if last := m.History[len(m.History)-1]; last != "ERROR: Connection reset by provider" {
		t.Fatalf("error not committed: %q", last)
	}
	errEvents := 0
	for _, e := range m.Timeline.Events {
		if e.Type == components.EventError {
			errEvents++
		}
	}
	if errEvents != 1 {
		t.Fatalf("expected 1 timeline error event, got %d", errEvents)
	}

	// Interrupt: engine returns empty chat_done. The in-flight round's partial
	// streamed content and reasoning are preserved as one final KYREX/Thought
	// pair; nothing is invented beyond what was streamed.
	before := len(m.History)
	m = applyEvents(m, MsgFromEngine{Type: "chat_done"})
	if len(m.History) != before+2 {
		t.Fatalf("interrupt committed unexpected entries: %#v", m.History)
	}
	if got := m.History[len(m.History)-2]; got != "_Assistant:_\npartial answer" {
		t.Fatalf("interrupt assistant commit = %q", got)
	}
	if got := m.History[len(m.History)-1]; got != "_Thought:_\npartial thought" {
		t.Fatalf("interrupt thought commit = %q", got)
	}
	if strings.Contains(stripANSI(m.FullViewportContent(100)), "Model produced reasoning but no display content") {
		t.Fatal("engine placeholder leaked into transcript")
	}
}

// TestViewportFollowsBottomWhileStreaming asserts new committed content keeps
// the viewport anchored when the user is at the bottom.
func TestViewportFollowsBottomWhileStreaming(t *testing.T) {
	m := newConvModel()
	m.Viewport.Height = 6
	m.History = []string{"> task"}
	first := m.FullViewportContent(100)
	m.Viewport.SetContent(first)
	m._lastSetContent = first
	m.Viewport.GotoBottom()
	m.ScrollLock = false

	m, _, _ = m.handleEngineMsg(MsgFromEngine{Type: "token", Content: "first round content"})
	m, _, _ = m.handleEngineMsg(MsgFromEngine{Type: "token", Content: roundSeparator})
	m.flushViewport()

	if !m.Viewport.AtBottom() {
		t.Fatal("viewport did not follow new content while user is at bottom")
	}
	vp := m.Viewport.View()
	if !strings.Contains(vp, "first round content") {
		t.Fatalf("viewport missing committed content:\n%s", vp)
	}
}

// TestViewportPreservesScrollWhenScrolledUp asserts growing content does not
// yank a scrolled-up user back to the bottom.
func TestViewportPreservesScrollWhenScrolledUp(t *testing.T) {
	m := newConvModel()
	m.Viewport.Height = 6
	m.History = []string{"> task", "_Assistant:_\nshort"}
	content := m.FullViewportContent(100)
	m.Viewport.SetContent(content)
	m._lastSetContent = content
	m.Viewport.GotoBottom()
	m.Viewport.LineUp(1)
	m.ScrollLock = true
	if m.Viewport.AtBottom() {
		t.Fatal("test setup: expected to be scrolled above bottom")
	}

	m.History = append(m.History, "_Assistant:_\nnew content below")
	m._viewportDirty = true
	m.flushViewport()

	if m.Viewport.AtBottom() {
		t.Fatal("viewport jumped to bottom despite ScrollLock")
	}
}

// TestViewportFirstMessageRenders asserts the first streamed message renders
// correctly through the viewport pipeline.
func TestViewportFirstMessageRenders(t *testing.T) {
	m := newConvModel()
	m.Viewport.Height = 6
	m.HasSentFirstMessage = true
	m.History = nil

	m, _, _ = m.handleEngineMsg(MsgFromEngine{Type: "token", Content: "Hello from Kyrex."})
	m.flushViewport()

	vp := m.Viewport.View()
	if !strings.Contains(stripANSI(vp), "KYREX") || !strings.Contains(stripANSI(vp), "Hello from Kyrex.") {
		t.Fatalf("first message not rendered:\n%s", vp)
	}
}

// TestManyRoundsRenderCleanly asserts long KYREX/Thought sequences render
// without corruption: right block counts, no separator artifacts, Overview at
// the end, and a sane line count.
func TestManyRoundsRenderCleanly(t *testing.T) {
	m := newConvModel()
	m.History = []string{"> big task"}
	for i := 0; i < 6; i++ {
		m = applyEvents(m,
			MsgFromEngine{Type: "reasoning", Content: fmt.Sprintf("thought %d", i)},
			MsgFromEngine{Type: "token", Content: fmt.Sprintf("message %d", i)},
			MsgFromEngine{Type: "token", Content: roundSeparator},
		)
	}
	m = applyEvents(m,
		MsgFromEngine{Type: "reasoning", Content: "final thought"},
		MsgFromEngine{Type: "token", Content: "final message"},
		MsgFromEngine{Type: "chat_done", Content: "final message\n[Task Complete: Wrapped up.]"},
	)

	rendered, lines := m.HistoryContent(100)
	clean := stripANSI(rendered)
	if n := strings.Count(clean, "KYREX"); n != 7 {
		t.Errorf("expected 7 KYREX blocks, got %d:\n%s", n, clean)
	}
	if n := strings.Count(clean, "Thought"); n != 7 {
		t.Errorf("expected 7 Thought blocks, got %d:\n%s", n, clean)
	}
	if !strings.Contains(clean, "Overview") {
		t.Fatalf("missing final Overview:\n%s", clean)
	}
	if strings.Contains(clean, "\n\n---\n") {
		t.Fatalf("round separator leaked into render:\n%s", clean)
	}
	if !strings.HasSuffix(strings.TrimSpace(clean), "Wrapped up.") {
		t.Fatalf("Overview not last:\n%s", clean)
	}
	if lines < 10 {
		t.Fatalf("suspiciously small transcript (%d lines):\n%s", lines, clean)
	}
}

// TestTaskCompleteSummaryParsing covers the engine's own summary extraction.
func TestTaskCompleteSummaryParsing(t *testing.T) {
	cases := []struct {
		in   string
		want string
	}{
		{"Done.\n[Task Complete: Fixed it.]", "Fixed it."},
		{"No marker here.", ""},
		{"[Task Complete: Multi\nline\nsummary.]", "Multi\nline\nsummary."},
		{"", ""},
	}
	for _, c := range cases {
		if got := extractTaskCompleteSummary(c.in); got != c.want {
			t.Errorf("extractTaskCompleteSummary(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

// TestCompactThoughtBounding covers the presentation-layer smoothing used for
// extremely long reasoning.
func TestCompactThoughtBounding(t *testing.T) {
	short := "one\ntwo"
	if got := compactThought(short); got != short {
		t.Fatalf("short thought should pass through untouched, got %q", got)
	}
	var long strings.Builder
	for i := 0; i < 20; i++ {
		long.WriteString("line\n")
	}
	got := compactThought(long.String())
	if !strings.Contains(got, "+14 lines") {
		t.Fatalf("expected truncation marker, got:\n%s", got)
	}
}

// countThoughtRuns returns the longest run of consecutive _Thought:_ entries
// in history — the invariant the pacing policy exists to keep at 1.
func countThoughtRuns(history []string) int {
	run, best := 0, 0
	for _, h := range history {
		if strings.HasPrefix(h, "_Thought:_") {
			run++
			if run > best {
				best = run
			}
		} else {
			run = 0
		}
	}
	return best
}

// TestFiveReasoningRoundsDoNotStackThoughts proves the core pacing rule: five
// consecutive reasoning-only rounds must NOT produce five committed (or
// rendered) Thought blocks. One leading Thought may surface as orientation;
// the remaining chains coalesce in the live buffer and fold into the single
// Thought that attaches to the eventual KYREX answer.
func TestFiveReasoningRoundsDoNotStackThoughts(t *testing.T) {
	m := newConvModel()
	m.History = []string{"> task"}
	for i := 1; i <= 5; i++ {
		m = applyEvents(m,
			MsgFromEngine{Type: "reasoning", Content: fmt.Sprintf("internal round %d reasoning", i)},
			MsgFromEngine{Type: "token", Content: roundSeparator},
		)
	}
	m = applyEvents(m,
		MsgFromEngine{Type: "token", Content: "Here is the answer."},
		MsgFromEngine{Type: "chat_done", Content: "Here is the answer.\n[Task Complete: Answered.]"},
	)

	// Five reasoning events, but at most the first chain plus the chain
	// attached to the final content may become Thoughts — never five, and
	// never two in a row.
	thoughts := 0
	for _, h := range m.History {
		if strings.HasPrefix(h, "_Thought:_") {
			thoughts++
		}
	}
	if thoughts > 2 {
		t.Fatalf("5 reasoning rounds committed %d Thoughts, want <= 2:\n%#v", thoughts, m.History)
	}
	if run := countThoughtRuns(m.History); run > 1 {
		t.Fatalf("consecutive Thought run of %d in history:\n%#v", run, m.History)
	}

	// The suppressed chains are not lost: they coalesce into the Thought that
	// attaches to the final KYREX content, so the transcript still has the
	// useful orientation without the per-round noise.
	joined := strings.Join(m.History, "\n")
	if !strings.Contains(joined, "internal round 2 reasoning") ||
		!strings.Contains(joined, "internal round 5 reasoning") {
		t.Fatalf("coalesced reasoning missing from final Thought:\n%#v", m.History)
	}

	// Visible transcript proves the user-facing claim: few Thought blocks,
	// no run, KYREX remains the primary voice with the answer present.
	rendered, _ := m.HistoryContent(100)
	clean := stripANSI(rendered)
	if n := strings.Count(clean, "Thought"); n > 2 {
		t.Fatalf("render shows %d Thought blocks for 5 reasoning rounds:\n%s", n, clean)
	}
	if n := strings.Count(clean, "KYREX"); n != 1 {
		t.Fatalf("expected the single KYREX answer block, got %d:\n%s", n, clean)
	}
	if !strings.Contains(clean, "Here is the answer.") {
		t.Fatalf("KYREX answer missing from render:\n%s", clean)
	}
}

// TestKYREXContentSupersedesAndAllowsLaterThought proves a meaningful KYREX
// content event interrupts the current reasoning state (ending the Thought
// burst) and re-opens the latch, so a later reasoning chain may surface again
// as its own Thought — the normal Thought → KYREX → Thought → KYREX rhythm,
// with the coalesced suppressed chain attaching to the first content round.
func TestKYREXContentSupersedesAndAllowsLaterThought(t *testing.T) {
	m := newConvModel()
	m.History = []string{"> task"}
	m = applyEvents(m,
		MsgFromEngine{Type: "reasoning", Content: "why does the build fail?"},
		MsgFromEngine{Type: "token", Content: roundSeparator},
		// Second bare chain is coalesced, not surfaced as its own block.
		MsgFromEngine{Type: "reasoning", Content: "maybe npm needs a flag"},
		MsgFromEngine{Type: "token", Content: roundSeparator},
		// Meaningful KYREX content: it supersedes the reasoning burst and
		// absorbs the coalesced chain as this content's Thought.
		MsgFromEngine{Type: "token", Content: "I checked the build configuration."},
		MsgFromEngine{Type: "token", Content: roundSeparator},
		// Content landed, so a fresh reasoning chain may surface again.
		MsgFromEngine{Type: "reasoning", Content: "now verify the fix"},
		MsgFromEngine{Type: "token", Content: "The build now passes with the corrected flag."},
		MsgFromEngine{Type: "chat_done", Content: "The build now passes with the corrected flag.\n[Task Complete: Fixed the build.]"},
	)

	want := []string{
		"> task",
		"_Thought:_\nwhy does the build fail?",
		"_Assistant:_\nI checked the build configuration.",
		"_Thought:_\nmaybe npm needs a flag",
		"_Assistant:_\nThe build now passes with the corrected flag.",
		"_Thought:_\nnow verify the fix",
		"_Overview:_\nFixed the build.",
	}
	if len(m.History) != len(want) {
		t.Fatalf("history len = %d, want %d:\n%#v", len(m.History), len(want), m.History)
	}
	for i, w := range want {
		if m.History[i] != w {
			t.Errorf("history[%d] = %q\n       want %q", i, m.History[i], w)
		}
	}
	// Content interruptions mean Thoughts are never adjacent.
	if run := countThoughtRuns(m.History); run > 1 {
		t.Fatalf("consecutive Thought run of %d:\n%#v", run, m.History)
	}

	// Rendered rhythm: Thought → KYREX → Thought → KYREX → Thought → Overview.
	rendered, _ := m.HistoryContent(100)
	clean := stripANSI(rendered)
	t1 := strings.Index(clean, "why does the build fail?")
	k1 := strings.Index(clean, "I checked the build configuration.")
	t2 := strings.Index(clean, "maybe npm needs a flag")
	k2 := strings.Index(clean, "The build now passes with the corrected flag.")
	t3 := strings.Index(clean, "now verify the fix")
	ov := strings.Index(clean, "Overview")
	if !(t1 >= 0 && t1 < k1 && k1 < t2 && t2 < k2 && k2 < t3 && t3 < ov) {
		t.Fatalf("Thought/KYREX rhythm broken: t1=%d k1=%d t2=%d k2=%d t3=%d ov=%d\n%s", t1, k1, t2, k2, t3, ov, clean)
	}
}