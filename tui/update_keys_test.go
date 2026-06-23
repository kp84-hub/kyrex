package tui

import (
	"reflect"
	"testing"

	tea "github.com/charmbracelet/bubbletea"
)

func TestFilterCommands(t *testing.T) {
	cases := []struct {
		input    string
		expected []string
	}{
		{"", []string{"/clear", "/new", "/branch", "/checkout", "/tree", "/undo", "/bookmark", "/export", "/skill", "/spawn", "/mcp", "/model", "/help", "/benchmark", "/metrics", "/setup", "/testcopy"}},
		{"c", []string{"/clear", "/checkout"}},
		{"m", []string{"/mcp", "/model", "/metrics"}},
		{"mo", []string{"/model"}},
		{"xyz", nil},
	}

	for _, c := range cases {
		got := filterCommands(c.input)
		if !reflect.DeepEqual(got, c.expected) {
			t.Errorf("filterCommands(%q) = %v, want %v", c.input, got, c.expected)
		}
	}
}

func TestCommandPickerActivation(t *testing.T) {
	m := NewModel(nil)

	// Typing "/" in an empty textarea should activate the picker.
	m, _, handled := m.handleKeyMsg(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'/'}}, m._lastKeyTime)
	if !handled {
		t.Fatal(`expected "/" to be handled`)
	}
	if !m._cmdPickerActive {
		t.Fatal("expected command picker to be active")
	}
	if m.Textarea.Value() != "/" {
		t.Fatalf(`expected textarea value "/", got %q`, m.Textarea.Value())
	}

	// Typing "c" should filter to commands starting with "c".
	m, _, handled = m.handleKeyMsg(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'c'}}, m._lastKeyTime)
	if !handled {
		t.Fatal(`expected "c" to be handled while picker is active`)
	}
	if !reflect.DeepEqual(m._cmdPickerItems, []string{"/clear", "/checkout"}) {
		t.Fatalf("unexpected filtered items: %v", m._cmdPickerItems)
	}
	if m.Textarea.Value() != "/c" {
		t.Fatalf(`expected textarea value "/c", got %q`, m.Textarea.Value())
	}

	// Down arrow should move selection.
	m, _, handled = m.handleKeyMsg(tea.KeyMsg{Type: tea.KeyDown}, m._lastKeyTime)
	if !handled || m._cmdPickerIndex != 1 {
		t.Fatalf("expected selection to move down, index = %d", m._cmdPickerIndex)
	}

	// Enter should fill the selected command.
	m, _, handled = m.handleKeyMsg(tea.KeyMsg{Type: tea.KeyEnter}, m._lastKeyTime)
	if !handled {
		t.Fatal("expected Enter to be handled")
	}
	if m._cmdPickerActive {
		t.Fatal("expected picker to close after selection")
	}
	if m.Textarea.Value() != "/checkout" {
		t.Fatalf(`expected textarea value "/checkout", got %q`, m.Textarea.Value())
	}
}

func TestCommandPickerCancel(t *testing.T) {
	m := NewModel(nil)
	m.activateCommandPicker("")

	m, _, handled := m.handleKeyMsg(tea.KeyMsg{Type: tea.KeyEsc}, m._lastKeyTime)
	if !handled || m._cmdPickerActive {
		t.Fatal("expected Esc to close picker")
	}
	if m.Textarea.Value() != "" {
		t.Fatalf("expected textarea to be cleared, got %q", m.Textarea.Value())
	}
}
