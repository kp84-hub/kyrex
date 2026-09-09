package tui

// Approval lifecycle regression suite.
//
// These tests pin the TUI approval-state contract:
//   - a pending approval renders as exactly one live state/card
//   - a second approval replaces the first live state
//   - y resolves the pending approval and the blocked operation RESUMES
//   - n resolves it and the blocked operation TERMINATES cleanly
//   - no pending approval remains after resolution
//   - subsequent engine events are processed after approval
//   - multiple sequential approvals do not deadlock
//   - the existing y/n protocol and safety semantics are unchanged
//   - programmatic resets (chat_done, resetTurnState, turn re-entry) never
//     strand an engine-side gate: the pending confirmation is settled as an
//     explicit denial, never silently wiped and never auto-approved
//   - repeated Gate B sweep detections present exactly one live card while
//     prior transcript/audit history stays intact
//
// The engine side of every Gate A test is a deterministic fake: a "blocked
// operation" registers a buffered channel per confirm_request and only
// receives its decision when the TUI's SendFunc delivers the matching
// confirm_response — proving the actual wait/resume/terminate behaviour
// rather than just rendered text. Every decision assertion is
// timeout-guarded so a deadlock fails the test instead of hanging it.

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	tea "github.com/charmbracelet/bubbletea"

	"github.com/kp84-hub/kx/internal/rift"
)

// ── deterministic fake engine ──────────────────────────────────────────

// approvalFakeGate models the engine-side blocked operation for one
// confirm_request. Its decided channel is the exact moment the approval
// decision reaches the waiting operation.
type approvalFakeGate struct {
	id      string
	decided chan bool // buffered(1): receives the decision when it arrives
}

// approvalFakeEngine mimics core_bridge.stdin_thread: it records every frame
// the TUI sends and resolves any registered gate id.
type approvalFakeEngine struct {
	mu    sync.Mutex
	sent  []map[string]interface{}
	gates map[string]*approvalFakeGate
}

func newApprovalFakeEngine() *approvalFakeEngine {
	return &approvalFakeEngine{gates: map[string]*approvalFakeGate{}}
}

func (f *approvalFakeEngine) registerGate(id string) *approvalFakeGate {
	g := &approvalFakeGate{id: id, decided: make(chan bool, 1)}
	f.mu.Lock()
	f.gates[id] = g
	f.mu.Unlock()
	return g
}

func (f *approvalFakeEngine) send(v interface{}) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	var frame map[string]interface{}
	switch fr := v.(type) {
	case map[string]interface{}:
		frame = fr
	case map[string]string:
		// handleSubmit emits map[string]string chat/command frames.
		frame = make(map[string]interface{}, len(fr))
		for k, val := range fr {
			frame[k] = val
		}
	}
	f.sent = append(f.sent, frame)
	if t, _ := frame["type"].(string); t == "confirm_response" {
		id, _ := frame["id"].(string)
		if g, ok := f.gates[id]; ok {
			approved, _ := frame["approved"].(bool)
			select {
			case g.decided <- approved:
			default:
			}
		}
	}
	return nil
}

func (f *approvalFakeEngine) frame(i int) map[string]interface{} {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.sent[i]
}

func (f *approvalFakeEngine) countType(typ string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, fr := range f.sent {
		if t, _ := fr["type"].(string); t == typ {
			n++
		}
	}
	return n
}

// ── helpers ────────────────────────────────────────────────────────────

func newApprovalTestModel(f *approvalFakeEngine) Model {
	m := NewModel(nil)
	if f != nil {
		m = NewModel(f.send)
	}
	m.Width = 120
	m.Height = 40
	m.Viewport.Width = 120
	m.Viewport.Height = 30
	m.HasSentFirstMessage = true
	return m
}

// injectConfirm drives a confirm_request through the same path the engine
// bridge uses (MsgFromEngine → handleEngineMsg → handleConfirmRequest).
func injectConfirm(t *testing.T, m Model, id, path string) Model {
	t.Helper()
	nm, _ := m.Update(MsgFromEngine{
		Type: "confirm_request",
		ID:   id,
		Path: path,
		Diff: "--- a/" + path + "\n+++ b/" + path + "\n+change",
	})
	return nm.(Model)
}

// pressDecision delivers a y/n keypress through the normal key gate.
func pressDecision(t *testing.T, m Model, key string) Model {
	t.Helper()
	nm, _, _ := m.handleKeyMsg(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune(key)}, time.Time{})
	return nm
}

// awaitGateDecision asserts the decision REACHED the blocked operation within
// the timeout — a strand/hang fails here instead of hanging the suite.
func awaitGateDecision(t *testing.T, g *approvalFakeGate, want bool) {
	t.Helper()
	select {
	case got := <-g.decided:
		if got != want {
			t.Fatalf("gate %s received decision %v, want %v", g.id, got, want)
		}
	case <-time.After(3 * time.Second):
		t.Fatalf("gate %s never received its decision — blocked operation stranded", g.id)
	}
}

func assertNoDecision(t *testing.T, g *approvalFakeGate) {
	t.Helper()
	select {
	case got := <-g.decided:
		t.Fatalf("gate %s unexpectedly received decision %v", g.id, got)
	default:
	}
}

func assertNoPendingApproval(t *testing.T, m Model) {
	t.Helper()
	if m.ConfirmID != "" {
		t.Fatalf("pending Gate A approval still present after resolution: %q", m.ConfirmID)
	}
	if m.SweepActive {
		t.Fatal("pending Gate B sweep still present after resolution")
	}
}

func historyJoined(m Model) string { return strings.Join(m.History, "\n") }

// ── 1: one active approval card ────────────────────────────────────────

func TestPendingApprovalRendersSingleCard(t *testing.T) {
	f := newApprovalFakeEngine()
	m := newApprovalTestModel(f)
	g := f.registerGate("g1")
	m = injectConfirm(t, m, "g1", "/tmp/a.txt")

	if m.ConfirmID != "g1" {
		t.Fatalf("ConfirmID = %q, want g1", m.ConfirmID)
	}
	if got := strings.Count(m.View(), "[!] CONFIRM CHANGES"); got != 1 {
		t.Fatalf("expected exactly one approval card in viewport, got %d", got)
	}

	// While an approval is live, other input must not leak into the textarea
	// or destroy the pending state.
	before := m.Textarea.Value()
	m, _, _ = m.handleKeyMsg(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("stray typing")}, time.Time{})
	if m.Textarea.Value() != before {
		t.Fatalf("stray keys leaked into textarea during pending approval: %q", m.Textarea.Value())
	}
	if m.ConfirmID != "g1" {
		t.Fatal("pending approval destroyed by stray keys")
	}
	_ = g
}

// ── 2: second approval replaces the first live state ───────────────────

func TestSecondApprovalReplacesFirstLiveState(t *testing.T) {
	f := newApprovalFakeEngine()
	m := newApprovalTestModel(f)
	g1 := f.registerGate("g1")
	g2 := f.registerGate("g2")

	m = injectConfirm(t, m, "g1", "/tmp/a.txt")
	m = injectConfirm(t, m, "g2", "/tmp/b.txt")

	if m.ConfirmID != "g2" {
		t.Fatalf("live approval = %q, want g2 (replacement)", m.ConfirmID)
	}
	if got := strings.Count(m.View(), "[!] CONFIRM CHANGES"); got != 1 {
		t.Fatalf("second approval appended a second card: got %d approval cards", got)
	}

	// The superseded gate is settled as a denial — never left blocked.
	awaitGateDecision(t, g1, false)
	assertNoDecision(t, g2)
}

// ── 3: y resolves; blocked operation resets and resumes ────────────────

func TestYResolvesApprovalAndOperationResumes(t *testing.T) {
	f := newApprovalFakeEngine()
	m := newApprovalTestModel(f)
	g := f.registerGate("g1")
	m = injectConfirm(t, m, "g1", "/tmp/a.txt")

	m = pressDecision(t, m, "y")

	// The waiting operation resumed with an explicit approve.
	awaitGateDecision(t, g, true)
	assertNoPendingApproval(t, m)
	if m.ConfirmID != "" || m.ConfirmPath != "" {
		t.Fatalf("confirm fields not cleared after y: id=%q path=%q", m.ConfirmID, m.ConfirmPath)
	}

	// The approval result is recorded in history.
	if !strings.Contains(historyJoined(m), "Approved change to: /tmp/a.txt") {
		t.Fatalf("approval result missing from history:\n%s", historyJoined(m))
	}
}

// ── 4: n resolves; blocked operation terminates cleanly ────────────────

func TestNResolvesApprovalAndOperationTerminates(t *testing.T) {
	f := newApprovalFakeEngine()
	m := newApprovalTestModel(f)
	g := f.registerGate("g1")
	m = injectConfirm(t, m, "g1", "/tmp/a.txt")

	m = pressDecision(t, m, "n")

	// The waiting operation terminated with an explicit deny.
	awaitGateDecision(t, g, false)
	assertNoPendingApproval(t, m)

	// The rejection is recorded.
	if !strings.Contains(historyJoined(m), "Rejected change to: /tmp/a.txt") {
		t.Fatalf("rejection missing from history:\n%s", historyJoined(m))
	}
}

// ── 5: no pending approval remains after resolution ───────────────────

func TestNoPendingApprovalAfterResolution(t *testing.T) {
	f := newApprovalFakeEngine()
	for _, key := range []string{"y", "n"} {
		m := newApprovalTestModel(f)
		g := f.registerGate("g1")
		m = injectConfirm(t, m, "g1", "/tmp/a.txt")
		m = pressDecision(t, m, key)
		awaitGateDecision(t, g, key == "y")
		assertNoPendingApproval(t, m)
	}

	// Sweep resolution too.
	m := newApprovalTestModel(f)
	m.SweepActive = true
	m.SweepChanges = []rift.Change{{Path: "a.go", Kind: rift.Modified}}
	m._sweepCardStart, m._sweepCardEnd = 0, 0
	m = pressDecision(t, m, "n")
	assertNoPendingApproval(t, m)
}

// ── 6: engine events are processed after approval ─────────────────────

func TestEngineEventsProcessedAfterApproval(t *testing.T) {
	f := newApprovalFakeEngine()
	m := newApprovalTestModel(f)
	g := f.registerGate("g1")
	m = injectConfirm(t, m, "g1", "/tmp/a.txt")
	m = pressDecision(t, m, "y")
	awaitGateDecision(t, g, true)

	// Tool result arrives and is processed normally.
	nm, _ := m.Update(MsgFromEngine{
		Type:   "tool_result",
		ID:     "tool_1",
		Result: map[string]interface{}{"status": "ok"},
	})
	m = nm.(Model)
	if m.ToolResult != "OK" {
		t.Fatalf("tool result not processed after approval: ToolResult=%q", m.ToolResult)
	}

	// Streaming continues.
	nm, _ = m.Update(MsgFromEngine{Type: "token", Content: "final answer."})
	m = nm.(Model)
	if m.CurrToken != "final answer." {
		t.Fatalf("token not accumulated after approval: %q", m.CurrToken)
	}

	// Turn completion lands and does not resurrect approval state.
	nm, _ = m.Update(MsgFromEngine{Type: "chat_done", Content: "final answer.", Reasoning: ""})
	m = nm.(Model)
	if !strings.Contains(historyJoined(m), "final answer.") {
		t.Fatalf("chat_done content missing from history:\n%s", historyJoined(m))
	}
	assertNoPendingApproval(t, m)
}

// ── 7: multiple sequential approvals do not deadlock ──────────────────

func TestMultipleSequentialApprovalsNoDeadlock(t *testing.T) {
	f := newApprovalFakeEngine()
	m := newApprovalTestModel(f)

	for i := 0; i < 5; i++ {
		id := fmt.Sprintf("g%d", i)
		g := f.registerGate(id)
		m = injectConfirm(t, m, id, "/tmp/f.txt")

		key, want := "n", false
		if i%2 == 0 {
			key, want = "y", true
		}
		m = pressDecision(t, m, key)
		awaitGateDecision(t, g, want) // timeout-guarded: a deadlock fails here
		assertNoPendingApproval(t, m)
	}
}

// ── 8: existing approval semantics unchanged ──────────────────────────

func TestExistingApprovalSemanticsUnchanged(t *testing.T) {
	f := newApprovalFakeEngine()
	m := newApprovalTestModel(f)

	// y → confirm_response {type, id, approved:true} — exact legacy payload.
	g1 := f.registerGate("g1")
	m = injectConfirm(t, m, "g1", "/tmp/a.txt")
	m = pressDecision(t, m, "y")
	awaitGateDecision(t, g1, true)
	yFrame := f.frame(0)
	if yFrame["type"] != "confirm_response" || yFrame["id"] != "g1" || yFrame["approved"] != true {
		t.Fatalf("y payload drifted from the approval protocol: %#v", yFrame)
	}

	// n → confirm_response {type, id, approved:false}.
	g2 := f.registerGate("g2")
	m = injectConfirm(t, m, "g2", "/tmp/b.txt")
	m = pressDecision(t, m, "n")
	awaitGateDecision(t, g2, false)
	nFrame := f.frame(1)
	if nFrame["type"] != "confirm_response" || nFrame["id"] != "g2" || nFrame["approved"] != false {
		t.Fatalf("n payload drifted from the approval protocol: %#v", nFrame)
	}

	// Deletion confirmations are never auto-approved, even with auto-approve on.
	m2 := m
	m2.AutoApprove = true
	m2.AutoApproveDelay = time.Second
	_, cmd, _ := m2.handleConfirmRequest(MsgFromEngine{
		Type:  "confirm_request",
		ID:    "del1",
		Path:  "rm -rf /tmp/x",
		Diff:  "/tmp/x",
		Value: "deletion",
	})
	if cmd != nil {
		t.Fatal("deletion confirmation must not arm the auto-approve timer")
	}

	// Non-destructive edit with auto-approve on still auto-approves.
	m3 := newApprovalTestModel(f)
	m3.AutoApprove = true
	m3.AutoApproveDelay = time.Second
	g3 := f.registerGate("e1")
	m3, cmd, _ = m3.handleConfirmRequest(MsgFromEngine{
		Type: "confirm_request",
		ID:   "e1",
		Path: "/tmp/c.txt",
		Diff: "+change",
	})
	if cmd == nil {
		t.Fatal("edit confirmation must arm the auto-approve timer when enabled")
	}
	nm, _ := m3.Update(AutoApproveFireMsg{ConfirmID: "e1"})
	m3 = nm.(Model)
	awaitGateDecision(t, g3, true)
	if m3.ConfirmID != "" {
		t.Fatal("auto-approve did not clear pending state")
	}
}

// ── 9: chat_done while Gate A pending → denied, never stranded ────────

func TestChatDoneWhileGatePendingResolvesAsDenied(t *testing.T) {
	f := newApprovalFakeEngine()
	m := newApprovalTestModel(f)
	g := f.registerGate("g1")
	m = injectConfirm(t, m, "g1", "/tmp/a.txt")

	nm, _ := m.Update(MsgFromEngine{Type: "chat_done", Content: "done", Reasoning: ""})
	m = nm.(Model)

	// The blocked operation received its decision (deny) — no 300s hang.
	awaitGateDecision(t, g, false)
	assertNoPendingApproval(t, m)
	if !strings.Contains(historyJoined(m), "dismissed, not applied") {
		t.Fatalf("dismissal not recorded in history:\n%s", historyJoined(m))
	}
}

// ── 10: resetTurnState while Gate A pending → denied, never stranded ──

func TestResetTurnStateWhileGatePendingResolvesAsDenied(t *testing.T) {
	f := newApprovalFakeEngine()
	m := newApprovalTestModel(f)
	g := f.registerGate("g1")
	m = injectConfirm(t, m, "g1", "/tmp/a.txt")

	m.resetTurnState()

	awaitGateDecision(t, g, false)
	assertNoPendingApproval(t, m)
	if !strings.Contains(historyJoined(m), "dismissed, not applied") {
		t.Fatalf("dismissal not recorded after reset:\n%s", historyJoined(m))
	}
}

// ── 11: turn re-entry/submission while Gate A pending → denied ────────

func TestTurnReentryWhileGatePendingResolvesAsDenied(t *testing.T) {
	f := newApprovalFakeEngine()
	m := newApprovalTestModel(f)
	g := f.registerGate("g1")
	m = injectConfirm(t, m, "g1", "/tmp/a.txt")
	m.Textarea.SetValue("next prompt")

	m, _, _ = m.handleSubmit(tea.KeyMsg{Type: tea.KeyEnter}, time.Time{})

	// The stranded gate got its denial (handleSubmit emits the chat frame
	// first; the engine's stdin_thread intercepts the confirm_response
	// directly, so the gate resolves before the queued chat is consumed) and
	// the new turn proceeded normally.
	awaitGateDecision(t, g, false)
	assertNoPendingApproval(t, m)
	if !strings.Contains(historyJoined(m), "> next prompt") {
		t.Fatalf("new turn did not proceed after settlement:\n%s", historyJoined(m))
	}
	denied := false
	for i := 0; i < len(f.sent); i++ {
		fr := f.frame(i)
		if fr["type"] == "confirm_response" && fr["id"] == "g1" && fr["approved"] == false {
			denied = true
			break
		}
	}
	if !denied {
		t.Fatalf("no explicit deny decision was sent to the engine for g1: %#v", f.sent)
	}
	if f.countType("chat") != 1 {
		t.Fatalf("expected exactly one chat frame after settlement, got %d", f.countType("chat"))
	}
}

// ── 12: Gate B sweep card replaced, never appended ────────────────────

func TestSweepCardReplacedNotAppended(t *testing.T) {
	m := newApprovalTestModel(nil)
	m.History = []string{"turn one transcript", "keep this line"}

	m.presentSweepCard([]rift.Change{
		{Path: "a.go", Kind: rift.Modified},
		{Path: "b.go", Kind: rift.Untracked},
	})
	if got := strings.Count(historyJoined(m), "bypassed the diff gate"); got != 1 {
		t.Fatalf("first sweep must render one card, got %d", got)
	}

	m.presentSweepCard([]rift.Change{
		{Path: "c.go", Kind: rift.Modified},
	})

	joined := historyJoined(m)
	if got := strings.Count(joined, "bypassed the diff gate"); got != 1 {
		t.Fatalf("re-detected sweep appended a second card: got %d cards", got)
	}
	// The old pending list was replaced by the new one.
	if strings.Contains(joined, "a.go") || strings.Contains(joined, "b.go") {
		t.Fatalf("superseded sweep change list was not replaced:\n%s", joined)
	}
	if !strings.Contains(joined, "c.go") {
		t.Fatalf("new sweep change list missing:\n%s", joined)
	}
	// Prior transcript / audit history is untouched.
	if !strings.Contains(joined, "turn one transcript") || !strings.Contains(joined, "keep this line") {
		t.Fatalf("prior history was clobbered by card replacement:\n%s", joined)
	}
}

// ── 13: repeated git-backed detection → exactly one live card ─────────

func gitCmd(t *testing.T, dir string, args ...string) {
	t.Helper()
	cmd := exec.Command("git", append([]string{"-C", dir}, args...)...)
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("git %v failed: %v\n%s", args, err, out)
	}
}

func makeGitTree(t *testing.T) (source, clone string) {
	t.Helper()
	source = t.TempDir()
	if err := os.WriteFile(filepath.Join(source, "keep.txt"), []byte("base\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	gitCmd(t, source, "init", "-q")
	gitCmd(t, source, "add", "-A")
	gitCmd(t, source, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "initial")

	clone = t.TempDir()
	if err := os.WriteFile(filepath.Join(clone, "keep.txt"), []byte("base\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	gitCmd(t, clone, "init", "-q")
	gitCmd(t, clone, "add", "-A")
	gitCmd(t, clone, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "clone")
	return source, clone
}

func TestRepeatedSweepDetectionSingleLiveCard(t *testing.T) {
	source, clone := makeGitTree(t)

	m := newApprovalTestModel(nil)
	m.Workspace = &rift.Workspace{Root: clone, Source: source}
	m.WorkspaceMgr = rift.New()

	// Turn 1 leaves an unmerged change.
	if err := os.WriteFile(filepath.Join(clone, "one.txt"), []byte("1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if !m.detectUnmergedChanges() {
		t.Fatal("first detection reported no changes")
	}
	if got := strings.Count(historyJoined(m), "bypassed the diff gate"); got != 1 {
		t.Fatalf("first detection must render one card, got %d", got)
	}
	if !strings.Contains(historyJoined(m), "one.txt") {
		t.Fatalf("live sweep list missing after first detection:\n%s", historyJoined(m))
	}

	// Turn 2 streams a normal transcript line, adds another unmerged change,
	// and re-detects: the pending card is REPLACED, the transcript is kept.
	m.History = append(m.History, "_Assistant:_\nturn two answer")
	if err := os.WriteFile(filepath.Join(clone, "two.txt"), []byte("2\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if !m.detectUnmergedChanges() {
		t.Fatal("second detection reported no changes")
	}

	joined := historyJoined(m)
	if got := strings.Count(joined, "bypassed the diff gate"); got != 1 {
		t.Fatalf("repeated detections accumulated sweep cards: got %d", got)
	}
	// Both files are still unmerged, so both belong on the ONE live card.
	if !strings.Contains(joined, "one.txt") || !strings.Contains(joined, "two.txt") {
		t.Fatalf("live sweep list missing files:\n%s", joined)
	}
	// The transcript between the two detections survived the replacement.
	if !strings.Contains(joined, "turn two answer") {
		t.Fatalf("transcript between detections was clobbered:\n%s", joined)
	}
	if !m.SweepActive {
		t.Fatal("SweepActive not set while a card is live")
	}

	// y merges exactly the live list into the real project and clears state.
	m = pressDecision(t, m, "y")
	if m.SweepActive || len(m.SweepChanges) != 0 {
		t.Fatal("sweep state not cleared after y")
	}
	if m._sweepCardStart != 0 || m._sweepCardEnd != 0 {
		t.Fatalf("sweep card range not reset after y: [%d,%d)", m._sweepCardStart, m._sweepCardEnd)
	}
	for _, f := range []string{"one.txt", "two.txt"} {
		if _, err := os.Stat(filepath.Join(source, f)); err != nil {
			t.Fatalf("approved sweep change %s was not merged into the project: %v", f, err)
		}
	}
	assertNoPendingApproval(t, m)
}