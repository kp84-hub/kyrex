package tui

import (
	"strings"
	"testing"

	"github.com/charmbracelet/lipgloss"
)

func TestNormalFooterHint(t *testing.T) {
	tests := []struct {
		name       string
		width      int
		want       []string
		notWant    []string
	}{
		{
			name:    "narrow terminal",
			width:   59,
			want:    []string{"Ctrl+B sidebar", "/ commands"},
			notWant: []string{"Ctrl+Y copy", "Esc interrupt"},
		},
		{
			name:    "medium terminal",
			width:   75,
			want:    []string{"Ctrl+B sidebar", "Ctrl+Y copy", "/ commands"},
			notWant: []string{"Esc interrupt"},
		},
		{
			name: "wide terminal",
			width: 120,
			want: []string{"Ctrl+B sidebar", "Ctrl+Y copy", "Esc interrupt", "/ commands"},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := normalFooterHint(tt.width)
			for _, want := range tt.want {
				if !strings.Contains(got, want) {
					t.Errorf("normalFooterHint(%d) = %q; missing %q", tt.width, got, want)
				}
			}
			for _, notWant := range tt.notWant {
				if strings.Contains(got, notWant) {
					t.Errorf("normalFooterHint(%d) = %q; should not contain %q", tt.width, got, notWant)
				}
			}
		})
	}
}

// fitMetaLine must never let the joined line exceed the available width.
func TestFitMetaLineNoOverflow(t *testing.T) {
	// Mirrors the splash metadata line pieces (model, session, auto-approve).
	splashPieces := []string{
		"Model: claude-3-5-sonnet-20241022-with-an-extraordinarily-long-model-name",
		"Session: feature-x",
		"auto-approve: on",
	}
	// Mirrors the active-chat footer pieces (phase, brand, model, dims,
	// sending, timer, hint).
	footerPieces := []string{
		pillPhase.Render("⚡ EXECUTE"),
		brandStyle.Render("KYREX"),
		lipgloss.NewStyle().Foreground(accent).Render("☁  claude-3-5-sonnet-20241022"),
		lipgloss.NewStyle().Foreground(subtle).Render(" [120x40]"),
		timerStyle.Render("Sending.."),
		timerStyle.Render("(3s) Thinking."),
		lipgloss.NewStyle().Foreground(subtle).Render("  " + normalFooterHint(120)),
	}

	cases := []struct {
		name      string
		pieces    []string
		separator string
		widths    []int
	}{
		{name: "splash meta line", pieces: splashPieces, separator: "  •  ", widths: []int{72, 90, 120}},
		{name: "footer content", pieces: footerPieces, separator: " ", widths: []int{72, 90, 120}},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			for _, width := range tc.widths {
				got := fitMetaLine(tc.pieces, tc.separator, width)
				if w := lipgloss.Width(got); w > width {
					t.Errorf("width %d: fitMetaLine returned display width %d (> %d): %q", width, w, width, got)
				}
			}
		})
	}
}

// At a comfortably wide terminal nothing may be dropped: the full joined line
// must come back unchanged.
func TestFitMetaLineKeepsAllOnWide(t *testing.T) {
	pieces := []string{"Model: claude-3-5-sonnet-20241022", "Session: main", "auto-approve: off"}
	sep := "  •  "
	got := fitMetaLine(pieces, sep, 200)
	if want := strings.Join(pieces, sep); got != want {
		t.Fatalf("wide: got %q, want full join %q", got, want)
	}
}

// Dropping must remove whole pieces from the tail, never truncate mid-string.
func TestFitMetaLineDropsWholePieces(t *testing.T) {
	pieces := []string{"Model: super-long-model-name-that-overflows-everything", "Session: main", "auto-approve: off"}
	sep := "  •  "

	for _, width := range []int{10, 25, 40, 55, 72, 90} {
		got := fitMetaLine(pieces, sep, width)
		if got == "" {
			continue // even the first piece cannot fit; acceptable
		}
		matched := false
		for n := 0; n <= len(pieces); n++ {
			if got == strings.Join(pieces[:n], sep) {
				matched = true
				break
			}
		}
		if !matched {
			t.Errorf("width %d: %q is not a prefix-join of whole pieces (mid-string truncation?)", width, got)
		}
	}
}

// Width must be measured via lipgloss.Width (ignores ANSI codes), not raw
// len(), otherwise styled pieces are dropped far too aggressively.
func TestFitMetaLineIgnoresAnsi(t *testing.T) {
	red := lipgloss.NewStyle().Foreground(lipgloss.Color("9")).Render
	sep := " • "
	// Each piece displays as 4 cells but len() is far larger due to ANSI.
	pieces := []string{red("AAAA"), red("BBBB"), red("CCCC")}

	for _, width := range []int{15, 20, 30} {
		got := fitMetaLine(pieces, sep, width)
		if lipgloss.Width(got) > width {
			t.Errorf("width %d: display width %d exceeds it", width, lipgloss.Width(got))
		}
	}

	// Full join is 18 wide; at 14 the trailing "CCCC" piece must be dropped,
	// leaving the 11-wide "AAAA • BBBB" — not a mid-string cut.
	got := fitMetaLine(pieces, sep, 14)
	if want := 11; lipgloss.Width(got) != want {
		t.Errorf("width 14: got display width %d, want %d", lipgloss.Width(got), want)
	}
	if got == pieces[0] {
		t.Fatalf("width 14: expected more than the first piece to be retained")
	}
}

// Regression at the render level: the full-screen splash with a long model
// name must not produce any line wider than the terminal at 72 and 90 cols.
func TestSplashMetaLineFitsNarrow(t *testing.T) {
	longModel := "claude-3-5-sonnet-20241022-with-an-extraordinarily-long-model-name"

	for _, width := range []int{72, 90} {
		m := NewModel(nil)
		m.Width = width
		m.Height = 40
		m.HasSentFirstMessage = false
		m.Sidebar.CurrentModel = longModel
		m.LLMInfo = "Model: " + longModel

		out := m.RenderFullScreenSplash()
		for _, line := range strings.Split(strings.TrimRight(out, "\n"), "\n") {
			if w := lipgloss.Width(line); w > width {
				t.Errorf("splash width %d: line display width %d exceeds it: %q", width, w, line)
			}
		}
	}
}

// Regression at the render level: the active-chat footer must not overflow
// the terminal at 72 and 90 cols.
func TestFooterFitsNarrow(t *testing.T) {
	for _, width := range []int{72, 90} {
		m := NewModel(nil)
		m.Width = width
		m.Height = 40
		m.HasSentFirstMessage = true
		m.LLMInfo = "claude-3-5-sonnet-20241022"
		m.Phase = PhaseExecute
		m.IsSending = true
		m._timerActive = true
		m.IsThinking = true
		m.Timer = 3
		m.recalculateLayout()
		m.applyLayout(m.recalculateLayout())

		out := m.View()
		// The footer is the last line of the view.
		lines := strings.Split(strings.TrimRight(out, "\n"), "\n")
		if len(lines) == 0 {
			t.Fatalf("width %d: no output", width)
		}
		last := lines[len(lines)-1]
		if w := lipgloss.Width(last); w > width {
			t.Errorf("footer width %d: last line display width %d exceeds it: %q", width, w, last)
		}
	}
}
