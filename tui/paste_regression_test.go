package tui

// Deterministic tests for the paste-before-submit regression.
//
// Regression chain: 4b61188 added a 40ms "paste-burst" absorber that
// classified an Enter by the time elapsed since the previous key of ANY
// kind. It swallowed deliberate submits (fast typing, paste+Enter — six
// committed tests failed) while still mis-submitting pastes whose
// fragments were delayed past the window on terminals without
// bracketed-paste support. A timing window cannot serve both directions.
//
// The fix is structural: bracketed paste (bubbletea negotiates it by
// default) arrives as ONE KeyRunes message with Paste=true — newlines
// inside it are data and never generate key events, so pastes cannot
// auto-submit; a separate Enter/Ctrl+J is always a deliberate submit.
//
// All tests drive the real Update() path with synthetic tea.KeyMsg values —
// no timing dependence, no real terminal.

import (
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"
)

func pasteTestModel(t *testing.T) (Model, *[]interface{}) {
	t.Helper()
	calls := make([]interface{}, 0, 4)
	m := NewModel(func(v interface{}) error {
		calls = append(calls, v)
		return nil
	})
	nm, _ := m.Update(tea.WindowSizeMsg{Width: 120, Height: 40})
	return nm.(Model), &calls
}

// chatSends returns the content of every "chat" send captured so far —
// i.e. exactly the requests the prompt bar makes to the engine.
func chatSends(calls *[]interface{}) []string {
	out := []string{}
	for _, c := range *calls {
		if msg, ok := c.(map[string]string); ok && msg["type"] == "chat" {
			out = append(out, msg["content"])
		}
	}
	return out
}

func pasteBracketed(t *testing.T, m Model, text string) Model {
	t.Helper()
	nm, _ := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune(text), Paste: true})
	return nm.(Model)
}

func press(t *testing.T, m Model, k tea.KeyType) Model {
	t.Helper()
	nm, _ := m.Update(tea.KeyMsg{Type: k})
	return nm.(Model)
}

// ── 1. Pasting must never submit automatically — zero chat sends ──────

func TestBracketedPasteAloneNeverSubmits(t *testing.T) {
	cases := []struct {
		name string
		text string
	}{
		{"short single-line", "hello paste"},
		{"short multiline", "line one\nline two"},
		{"large multi-line block", strings.Repeat("pasted line with content\n", 40)},
		{"trailing newline", "pasted text\n"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			m, calls := pasteTestModel(t)
			m = pasteBracketed(t, m, tc.text)
			_ = m
			if got := chatSends(calls); len(got) != 0 {
				t.Fatalf("paste alone submitted %d chat request(s): %q", len(got), got)
			}
		})
	}
}

// ── 2. Multiline bracketed paste remains in the input ─────────────────

func TestMultilineBracketedPasteRemainsInInput(t *testing.T) {
	m, calls := pasteTestModel(t)
	text := "line one\nline two"
	m = pasteBracketed(t, m, text)
	if got := chatSends(calls); len(got) != 0 {
		t.Fatalf("multiline paste submitted: %q", got)
	}
	v := m.Textarea.Value()
	if !strings.Contains(v, "line one") || !strings.Contains(v, "line two") {
		t.Fatalf("pasted content missing from the prompt: %q", v)
	}
	if !strings.Contains(v, "\n") {
		t.Fatalf("pasted newline was not preserved in the prompt: %q", v)
	}
}

// ── 3. Paste + deliberate Enter sends exactly one complete request ────

func TestPastePlusDeliberateEnterSendsExactlyOnce(t *testing.T) {
	for _, k := range []tea.KeyType{tea.KeyEnter, tea.KeyCtrlJ} {
		m, calls := pasteTestModel(t)
		text := "please review this pasted prompt end to end"
		m = pasteBracketed(t, m, text)
		m = press(t, m, k)
		sends := chatSends(calls)
		if len(sends) != 1 {
			t.Fatalf("paste+Enter sent %d chat request(s), want exactly 1: %q", len(sends), sends)
		}
		if sends[0] != text {
			t.Fatalf("submitted content mismatch:\n got %q\nwant %q", sends[0], text)
		}
	}
}

// ── 4. Repeated/large pastes accumulate without dup/truncate/leak ─────

func TestRepeatedLargePastesSubmitComplete(t *testing.T) {
	m, calls := pasteTestModel(t)
	a := strings.Repeat("alpha fragment line\n", 30) // ≥20 runes → collapse
	b := strings.Repeat("beta fragment value\n", 30) // ≥20 runes → collapse
	m = pasteBracketed(t, m, a)
	m = pasteBracketed(t, m, b)
	if got := chatSends(calls); len(got) != 0 {
		t.Fatalf("pastes alone submitted: %q", got)
	}
	if m._realInputBuffer != a+b {
		t.Fatalf("accumulated buffer wrong:\n got %q\nwant %q", m._realInputBuffer, a+b)
	}
	m = press(t, m, tea.KeyEnter)
	sends := chatSends(calls)
	if len(sends) != 1 {
		t.Fatalf("submit sent %d chat request(s), want exactly 1: %q", len(sends), sends)
	}
	want := strings.TrimSpace(a + b)
	if sends[0] != want {
		t.Fatalf("repeated paste content wrong (dup/truncate/leak):\n got %q\nwant %q", sends[0], want)
	}
	if strings.Contains(sends[0], "[Pasted") {
		t.Fatalf("placeholder text leaked into submission: %q", sends[0])
	}
}

// ── 5. Pasted \r, \n, CRLF stay content — never act as Enter ──────────

func TestPastedLineEndingsRemainContent(t *testing.T) {
	cases := []struct {
		name string
		text string
		want string
	}{
		{"short LF", "a\nb", "a\nb"},
		{"short CRLF", "a\r\nb", "a\nb"},
		{"large LF", strings.Repeat("row\n", 30), strings.Repeat("row\n", 30)},
		{"large CRLF", strings.Repeat("row\r\n", 30), strings.Repeat("row\n", 30)},
		{"bare CR inside paste", "x\rmore\rrows here with padding", "x\nmore\nrows here with padding"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			m, calls := pasteTestModel(t)
			m = pasteBracketed(t, m, tc.text)
			if got := chatSends(calls); len(got) != 0 {
				t.Fatalf("pasted line endings submitted: %q", got)
			}
			m = press(t, m, tea.KeyEnter)
			sends := chatSends(calls)
			if len(sends) != 1 {
				t.Fatalf("want exactly 1 send after deliberate Enter, got %d: %q", len(sends), sends)
			}
			want := strings.TrimSpace(tc.want)
			if sends[0] != want {
				t.Fatalf("content mismatch:\n got %q\nwant %q", sends[0], want)
			}
		})
	}
}

// ── 6. Deliberate fast typed Enter still submits (no over-absorption) ─

func TestFastTypedEnterSubmits(t *testing.T) {
	m, calls := pasteTestModel(t)
	nm, _ := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("typed prompt")})
	m = nm.(Model)
	// Deliberately NO back-dating: the Enter arrives immediately after the
	// runes, exactly the case the removed 40ms absorber swallowed.
	m = press(t, m, tea.KeyEnter)
	sends := chatSends(calls)
	if len(sends) != 1 || sends[0] != "typed prompt" {
		t.Fatalf("fast typed Enter must submit exactly once: %q", sends)
	}
}

// ── 7. Approval gates consume their own keys before the submit path ───

func TestConfirmationGateConsumesItsKeys(t *testing.T) {
	m, calls := pasteTestModel(t)
	m.ConfirmID = "conf-1"
	m.ConfirmPath = "src/a.go"

	// 'n' is consumed by the gate: confirm_response goes out, no chat send.
	nm, _ := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("n")})
	m = nm.(Model)
	if m.ConfirmID != "" {
		t.Fatal("gate did not consume 'n' — ConfirmID still set")
	}
	if got := chatSends(calls); len(got) != 0 {
		t.Fatalf("gate keys leaked into the chat path: %q", got)
	}
	for _, c := range *calls {
		if msg, ok := c.(map[string]interface{}); ok && msg["type"] == "confirm_response" {
			if approved, _ := msg["approved"].(bool); approved {
				t.Fatalf("'n' must reject, got approved=%v", msg["approved"])
			}
			return
		}
	}
	t.Fatal("no confirm_response send — gate did not route its own key")

	// After the gate resolves, normal input submits normally.
	m = pasteBracketed(t, m, "after the gate")
	m = press(t, m, tea.KeyEnter)
	sends := chatSends(calls)
	if len(sends) != 1 || sends[0] != "after the gate" {
		t.Fatalf("post-gate submit broken: %q", sends)
	}
}
