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
		{"", []string{"/new", "/branch", "/checkout", "/tree", "/undo", "/bookmark", "/export", "/skill", "/spawn", "/mcp", "/model", "/help", "/setup"}},
		{"c", []string{"/checkout"}},
		{"m", []string{"/mcp", "/model"}},
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

	// Typing "m" should filter to commands starting with "m".
	m, _, handled = m.handleKeyMsg(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'m'}}, m._lastKeyTime)
	if !handled {
		t.Fatal(`expected "m" to be handled while picker is active`)
	}
	if !reflect.DeepEqual(m._cmdPickerItems, []string{"/mcp", "/model"}) {
		t.Fatalf("unexpected filtered items: %v", m._cmdPickerItems)
	}
	if m.Textarea.Value() != "/m" {
		t.Fatalf(`expected textarea value "/m", got %q`, m.Textarea.Value())
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
	if m.Textarea.Value() != "/model" {
		t.Fatalf(`expected textarea value "/model", got %q`, m.Textarea.Value())
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
