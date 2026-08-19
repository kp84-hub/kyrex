package tui

import (
	"strings"
	"testing"
	"time"

	tea "github.com/charmbracelet/bubbletea"
)

// Regression tests for the sidebar bleed caused by raw carriage returns in
// pasted prompts.
//
// Chain: bracketed paste delivers clipboard bytes verbatim (Windows/WSL2
// clipboards use CRLF) → the paste-collapse path (>= 20 runes) stored them
// raw, bypassing the textarea sanitizer → HistoryContent's >15-line
// truncation split on "\n" and orphaned a bare trailing "\r" on the kept
// line → the terminal interpreted it as cursor-to-column-0 and overprinted
// the sidebar with the rest of the prompt line.

func pasteRegressionModel(t *testing.T, w, h int) Model {
	t.Helper()
	m := NewModel(func(interface{}) error { return nil })
	nm, _ := m.Update(tea.WindowSizeMsg{Width: w, Height: h})
	return nm.(Model)
}

func pasteAndSubmit(t *testing.T, m Model, text string) Model {
	t.Helper()
	nm, _ := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune(text), Paste: true})
	m = nm.(Model)
	// Back-date the last keystroke so paste-burst detection (<50ms) does not
	// swallow the Enter as part of the paste.
	m._lastKeyTime = m._lastKeyTime.Add(-time.Second)
	nm, _ = m.Update(tea.KeyMsg{Type: tea.KeyEnter})
	m = nm.(Model)
	if len(m.History) == 0 {
		t.Fatal("submit did not happen — paste-burst back-dating broken?")
	}
	return m
}

func assertFrameHasNoCR(t *testing.T, label string, m Model) {
	t.Helper()
	out := m.View()
	if i := strings.IndexByte(out, '\r'); i >= 0 {
		lo := i - 40
		if lo < 0 {
			lo = 0
		}
		hi := i + 20
		if hi > len(out) {
			hi = len(out)
		}
		t.Errorf("%s: frame contains literal \\r at byte %d — terminal jumps to col 0 and overprints the sidebar\ncontext: %q",
			label, i, out[lo:hi])
	}
}

// Short CRLF paste: exercises lipgloss's own "\r\n" normalization plus the
// ingestion fix. History must never store raw \r.
func TestPastedCRLFPromptIsSanitized(t *testing.T) {
	dirty := "please fix this function:\r\n\tfunc main() {\r\n\t\tfmt.Println(\"hi\")\r\n\t}\r\nthanks"
	m := pasteRegressionModel(t, 140, 40)
	m = pasteAndSubmit(t, m, dirty)
	assertFrameHasNoCR(t, "after submit", m)
	for _, h := range m.History {
		if strings.Contains(h, "\r") {
			t.Errorf("history entry contains raw \\r: %q", h)
		}
	}
	if !strings.Contains(strings.Join(m.History, "\n"), "\tfunc main()") {
		t.Error("tabs should be preserved in the payload — only \\r is stripped")
	}
}

// >15 CRLF lines: triggers the "[+N lines]" truncation branch, which is the
// path that orphaned a bare "\r" on the displayed first line.
func TestLongCRLFPasteDoesNotBleedIntoSidebar(t *testing.T) {
	var b strings.Builder
	b.WriteString("fix all of these files:\r\n")
	for i := 0; i < 20; i++ {
		b.WriteString("\tsrc/module_" + string(rune('a'+i)) + ".go has a bug\r\n")
	}
	m := pasteRegressionModel(t, 140, 40)
	m = pasteAndSubmit(t, m, b.String())
	assertFrameHasNoCR(t, "after long CRLF submit", m)
}

// Old sessions / other ingestion paths may already hold CRLF text in History.
// The display layer must strip \r on its own.
func TestHistoryContentStripsPreexistingCR(t *testing.T) {
	m := pasteRegressionModel(t, 140, 40)
	m.HasSentFirstMessage = true
	var b strings.Builder
	b.WriteString("> legacy prompt line one\r")
	for i := 0; i < 20; i++ {
		b.WriteString("\nline\r")
	}
	m.History = append(m.History, b.String()[2:])
	m.History[len(m.History)-1] = "> " + m.History[len(m.History)-1]
	m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
	assertFrameHasNoCR(t, "legacy CRLF history", m)
	if clean := m.HistoryContentClean(m.Viewport.Width); strings.Contains(clean, "\r") {
		t.Error("HistoryContentClean leaked raw \\r")
	}
}
