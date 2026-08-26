package tui

import (
	"testing"
	"time"

	tea "github.com/charmbracelet/bubbletea"
)

func inputTestModel() (Model, *[]interface{}) {
	calls := make([]interface{}, 0, 1)
	m := NewModel(func(v interface{}) error {
		calls = append(calls, v)
		return nil
	})
	return m, &calls
}

func assertQuitCommand(t *testing.T, cmd tea.Cmd) {
	t.Helper()
	if cmd == nil {
		t.Fatal("expected quit command, got nil")
	}
	if _, ok := cmd().(tea.QuitMsg); !ok {
		t.Fatalf("expected tea.QuitMsg, got %T", cmd())
	}
}

func TestPhysicalEnterSubmits(t *testing.T) {
	m, calls := inputTestModel()
	nm, _ := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("normal prompt")})
	m = nm.(Model)
	nm, _ = m.Update(tea.KeyMsg{Type: tea.KeyEnter})
	m = nm.(Model)

	if len(*calls) != 1 {
		t.Fatalf("expected one submitted message, got %d", len(*calls))
	}
	if len(m.History) == 0 || m.Textarea.Value() != "" {
		t.Fatalf("Enter did not submit and reset textarea: history=%q textarea=%q", m.History, m.Textarea.Value())
	}
}

func TestFastPhysicalEnterSubmits(t *testing.T) {
	m, calls := inputTestModel()
	nm, _ := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("fast prompt")})
	m = nm.(Model)
	m._lastKeyTime = time.Now()
	nm, _ = m.Update(tea.KeyMsg{Type: tea.KeyEnter})
	m = nm.(Model)

	if len(*calls) != 1 || len(m.History) == 0 {
		t.Fatalf("fast Enter was not submitted: calls=%d history=%q", len(*calls), m.History)
	}
}

func TestCtrlCQuitsFromNormalChat(t *testing.T) {
	m := NewModel(nil)
	_, cmd, handled := m.handleKeyMsg(tea.KeyMsg{Type: tea.KeyCtrlC}, time.Time{})
	if !handled {
		t.Fatal("Ctrl+C was not handled")
	}
	assertQuitCommand(t, cmd)
}

func TestCtrlCQuitsFromActiveModalStates(t *testing.T) {
	cases := []struct {
		name string
		set  func(*Model)
	}{
		{"usage overlay", func(m *Model) { m._usageOverlayActive = true }},
		{"setup", func(m *Model) { m._setupActive = true; m._setupStep = 0 }},
		{"model picker", func(m *Model) { m._modelPickerActive = true }},
		{"mcp picker", func(m *Model) { m._mcpPickerActive = true }},
		{"race picker", func(m *Model) { m._raceModelPickerActive = true }},
		{"consult picker", func(m *Model) { m._consultModelPickerActive = true }},
		{"command picker", func(m *Model) { m._cmdPickerActive = true }},
		{"confirmation", func(m *Model) { m.ConfirmID = "confirm" }},
		{"sweep", func(m *Model) { m.SweepActive = true }},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			m := NewModel(nil)
			tc.set(&m)
			_, cmd, handled := m.handleKeyMsg(tea.KeyMsg{Type: tea.KeyCtrlC}, time.Time{})
			if !handled {
				t.Fatal("Ctrl+C was not handled")
			}
			assertQuitCommand(t, cmd)
		})
	}
}

func TestBracketedPasteThenSeparateEnterSubmits(t *testing.T) {
	m, calls := inputTestModel()
	paste := "please fix this large pasted prompt"
	nm, _ := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune(paste), Paste: true})
	m = nm.(Model)
	nm, _ = m.Update(tea.KeyMsg{Type: tea.KeyEnter})
	m = nm.(Model)

	if len(*calls) != 1 {
		t.Fatalf("separate Enter after paste did not submit: calls=%d", len(*calls))
	}
	if len(m.History) == 0 || m._realInputBuffer != "" {
		t.Fatalf("paste buffer was not submitted and cleared: history=%q buffer=%q", m.History, m._realInputBuffer)
	}
}

func TestPastedNewlineAndCRLFRemainContent(t *testing.T) {
	for _, tc := range []struct {
		name string
		text string
		want string
	}{
		{"newline", "paste line one\npaste line two with enough text", "paste line one\npaste line two with enough text"},
		{"crlf", "paste line one\r\npaste line two with enough text", "paste line one\npaste line two with enough text"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			m, calls := inputTestModel()
			nm, _ := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune(tc.text), Paste: true})
			m = nm.(Model)
			nm, _ = m.Update(tea.KeyMsg{Type: tea.KeyEnter})
			m = nm.(Model)
			if len(*calls) != 1 {
				t.Fatalf("paste was not submitted: calls=%d", len(*calls))
			}
			payload, ok := (*calls)[0].(map[string]string)
			if !ok || payload["content"] != tc.want {
				t.Fatalf("submitted content = %#v, want %q", (*calls)[0], tc.want)
			}
		})
	}
}

func TestPhysicalEnterIsDistinctFromPastedNewline(t *testing.T) {
	m, calls := inputTestModel()
	text := "content before newline\ncontent after newline"
	nm, _ := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune(text), Paste: true})
	m = nm.(Model)
	nm, _ = m.Update(tea.KeyMsg{Type: tea.KeyEnter})
	m = nm.(Model)

	if len(*calls) != 1 {
		t.Fatalf("physical Enter did not submit separately from pasted newline: calls=%d", len(*calls))
	}
	payload := (*calls)[0].(map[string]string)
	if payload["content"] != text {
		t.Fatalf("physical Enter changed pasted newline content: got %#v", payload["content"])
	}
}
