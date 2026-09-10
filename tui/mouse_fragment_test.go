package tui

import (
	"testing"
	"time"

	tea "github.com/charmbracelet/bubbletea"
)

// mouseTestModel builds a bare Model with no side-effect callbacks.
func mouseTestModel() Model {
	return NewModel(nil)
}

// dropCases are the SGR 1006 payloads -- complete reports, torn fragments,
// ESC-less fragments, and concatenated runs -- that must never reach the
// composer.
var dropCases = []string{
	// Complete SGR 1006 reports (upper and lower terminator).
	"\x1b[<65;14;44M",
	"\x1b[<0;0;35m",
	// Fragmented reports: torn mid-sequence, no terminator.
	"\x1b[<65;14;44",
	"\x1b[<65;1",
	"\x1b[<",
	// ESC-less fragments: the ESC byte was delivered in a separate chunk.
	"[<65;14;44M",
	"[<65;14;44",
	// Concatenated reports and report+fragment runs.
	"\x1b[<65;14;44M\x1b[<66;14;44m",
	"\x1b[<65;14;44M[<66;14;44",
	"[<65;14;44M\x1b[<66;14;44",
}

func TestSGRMouseFragmentClassifierDropsMouseData(t *testing.T) {
	for _, payload := range dropCases {
		if !isSGRMouseFragment(payload) {
			t.Errorf("isSGRMouseFragment(%q) = false, want true (mouse data must be classified)", payload)
		}
	}
}

func TestSGRMouseFragmentClassifierPreservesNormalText(t *testing.T) {
	preserved := []string{
		"hello",
		"fix the parser bug",
		"normal prompt",
		"1;2",
		"5;14;44M", // digits/;/M run WITHOUT the [< marker is ordinary text
		"a[<b",     // contains [< but is not mouse-shaped (letters)
		"x[<9",
		"/clear",
		"M", // a single torn terminator is ambiguous with the letter M
	}
	for _, payload := range preserved {
		if isSGRMouseFragment(payload) {
			t.Errorf("isSGRMouseFragment(%q) = true, want false (normal text must be preserved)", payload)
		}
	}
}

func TestHandleKeyMsgDropsMouseFragments(t *testing.T) {
	for _, payload := range dropCases {
		m := mouseTestModel()
		_, cmd, handled := m.handleKeyMsg(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune(payload)}, time.Time{})
		if !handled {
			t.Errorf("handleKeyMsg(%q) not handled; mouse fragment must be consumed", payload)
		}
		if cmd != nil {
			t.Errorf("handleKeyMsg(%q) returned a command (%T); expected nil", payload, cmd)
		}
		if v := m.Textarea.Value(); v != "" {
			t.Errorf("handleKeyMsg(%q) leaked into composer: textarea=%q", payload, v)
		}
	}
}

func TestUpdateDropsMouseFragmentsFromComposer(t *testing.T) {
	for _, payload := range dropCases {
		m := mouseTestModel()
		nm, _ := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune(payload)})
		m = nm.(Model)
		if v := m.Textarea.Value(); v != "" {
			t.Errorf("Update(%q) leaked into composer: textarea=%q", payload, v)
		}
	}
}

func TestUpdatePreservesNormalTextInput(t *testing.T) {
	cases := []struct {
		typed string
		want  string
	}{
		{"hello", "hello"},
		{"fix the parser bug", "fix the parser bug"},
		{"1;2", "1;2"},
		{"a[<b", "a[<b"},
	}
	for _, tc := range cases {
		m := mouseTestModel()
		nm, _ := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune(tc.typed)})
		m = nm.(Model)
		if v := m.Textarea.Value(); v != tc.want {
			t.Errorf("typing %q produced textarea=%q, want %q", tc.typed, v, tc.want)
		}
	}
}