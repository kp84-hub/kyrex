package tui

import (
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"
)

// Regression: on the full-screen splash, the textarea's real width must match
// the width-capped box it is rendered in (splashInputWidth). textarea.Update
// computes the internal viewport scroll offset from the real width — if it is
// wider than the rendered box, wrapped rows fall below the 1-line viewport
// and text past the first visible row is invisible while typing.
func TestSplashTextareaWidthMatchesRenderBox(t *testing.T) {
	m := NewModel(nil)
	m.Width = 120
	m.Height = 40
	m.HasSentFirstMessage = false

	m.applyLayout(m.recalculateLayout())

	// Textarea.Width() reports the text area only — SetWidth subtracts the
	// 2-cell prompt ("│ "), so the expected value is splashInputWidth-2-2.
	want := splashInputWidth(120) - 2 - 2
	if got := m.Textarea.Width(); got != want {
		t.Fatalf("splash textarea width = %d, want %d (splashInputWidth-2-prompt)", got, want)
	}
}

// Typing past the splash box width must scroll: the tail of the input stays
// visible in the rendered splash.
func TestSplashTextareaOverflowStaysVisible(t *testing.T) {
	m := NewModel(nil)
	m.Width = 120
	m.Height = 40
	m.HasSentFirstMessage = false
	m.applyLayout(m.recalculateLayout())

	// Distinctive sequence, longer than the box is wide.
	input := strings.Repeat("abcdefghijklmnopqrstuvwxyz0123456789", 4)
	for _, r := range input {
		m.Textarea, _ = m.Textarea.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{r}})
	}

	out := m.RenderFullScreenSplash()

	// The last character sits under the cursor (rendered separately), so check
	// for the 12 chars just before it — they must be visible, not scrolled off.
	tail := input[len(input)-13 : len(input)-1]
	if !strings.Contains(out, tail) {
		t.Fatalf("splash render does not show the end of the typed input %q", tail)
	}
}

// After the first real chat message, the textarea must return to full
// main-column width (layout re-applied on the splash→chat transition).
func TestTextareaWidthRestoredAfterFirstMessage(t *testing.T) {
	m := NewModel(nil)
	m.Width = 120
	m.Height = 40
	m.HasSentFirstMessage = false
	m.applyLayout(m.recalculateLayout())

	// Flip to chat mode and simulate the Update-time transition hook.
	m.HasSentFirstMessage = true
	if m.HasSentFirstMessage != m._lastHasSentFirstMessage {
		m._lastHasSentFirstMessage = m.HasSentFirstMessage
		m.applyLayout(m.recalculateLayout())
	}

	layout := m.recalculateLayout()
	want := layout.MainWidth - 2 - 2 // Width() excludes the 2-cell prompt
	if got := m.Textarea.Width(); got != want {
		t.Fatalf("chat textarea width = %d, want %d (MainWidth-2-prompt)", got, want)
	}
}
