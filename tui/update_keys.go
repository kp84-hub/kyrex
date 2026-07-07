package tui

import (
	"path/filepath"
"strconv"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/atotto/clipboard"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/kp84-hub/kx/internal/race"
	"github.com/kp84-hub/kx/tui/components"
)

// isMouseEscapeSequence reports whether s is an SGR 1006 mouse report
// such as ESC[<65;14;44M or ESC[<65;14;44m.
func isMouseEscapeSequence(s string) bool {
	if !strings.HasPrefix(s, "[<") {
		return false
	}
	if !(strings.HasSuffix(s, "M") || strings.HasSuffix(s, "m")) {
		return false
	}
	inner := s[2 : len(s)-1]
	parts := strings.Split(inner, ";")
	if len(parts) != 3 {
		return false
	}
	for _, p := range parts {
		if p == "" {
			return false
		}
		for _, r := range p {
			if r < '0' || r > '9' {
				return false
			}
		}
	}
	return true
}

// availableCommands is the full set of slash commands shown by the command picker.
var availableCommands = []string{
	"/new", "/branch", "/checkout", "/tree", "/undo", "/bookmark",
	"/export", "/skill", "/spawn", "/mcp", "/model", "/help", "/setup", "/autoapprove", "/race",
}

// filterCommands returns commands that start with the given input (case-insensitive).
func filterCommands(input string) []string {
	input = strings.ToLower(input)
	var filtered []string
	for _, cmd := range availableCommands {
		if strings.HasPrefix(strings.ToLower(cmd[1:]), input) {
			filtered = append(filtered, cmd)
		}
	}
	return filtered
}

// activateCommandPicker opens the slash-command picker with the given filter text.
func (m *Model) activateCommandPicker(input string) {
	m._cmdPickerActive = true
	m._cmdPickerInput = input
	m._cmdPickerItems = filterCommands(input)
	m._cmdPickerIndex = 0
	m.Textarea.SetValue("/" + input)
}

// closeCommandPicker hides the slash-command picker without changing the textarea.
func (m *Model) closeCommandPicker() {
	m._cmdPickerActive = false
	m._cmdPickerItems = nil
	m._cmdPickerIndex = 0
	m._cmdPickerInput = ""
}

// selectCommandPickerItem fills the highlighted command into the textarea.
func (m *Model) selectCommandPickerItem() {
	if m._cmdPickerIndex >= 0 && m._cmdPickerIndex < len(m._cmdPickerItems) {
		m.Textarea.SetValue(m._cmdPickerItems[m._cmdPickerIndex] + " ")
		m.closeCommandPicker()
	}
}

// handleCommandPickerKey handles keyboard input while the command picker is open.
func (m Model) handleCommandPickerKey(msg tea.KeyMsg) (Model, tea.Cmd, bool) {
	switch msg.Type {
	case tea.KeyEnter, tea.KeyTab:
		m.selectCommandPickerItem()
		return m, nil, true
	case tea.KeyUp:
		if m._cmdPickerIndex > 0 {
			m._cmdPickerIndex--
		}
		return m, nil, true
	case tea.KeyDown:
		if m._cmdPickerIndex < len(m._cmdPickerItems)-1 {
			m._cmdPickerIndex++
		}
		return m, nil, true
	case tea.KeyEsc:
		m.closeCommandPicker()
		m.Textarea.SetValue("")
		return m, nil, true
	case tea.KeyBackspace:
		if len(m._cmdPickerInput) > 0 {
			m._cmdPickerInput = m._cmdPickerInput[:len(m._cmdPickerInput)-1]
			m._cmdPickerItems = filterCommands(m._cmdPickerInput)
			m._cmdPickerIndex = 0
			m.Textarea.SetValue("/" + m._cmdPickerInput)
		} else {
			m.closeCommandPicker()
			m.Textarea.SetValue("")
		}
		return m, nil, true
	case tea.KeyRunes:
		s := string(msg.Runes)
		if s == " " || s == "\r" || s == "\n" {
			m.closeCommandPicker()
			m.Textarea.SetValue("/" + m._cmdPickerInput + " ")
			return m, nil, true
		}
		m._cmdPickerInput += s
		m._cmdPickerItems = filterCommands(m._cmdPickerInput)
		m._cmdPickerIndex = 0
		m.Textarea.SetValue("/" + m._cmdPickerInput)
		return m, nil, true
	default:
		// Close picker for other keys and let the textarea handle them.
		m.closeCommandPicker()
		return m, nil, false
	}
}

// handleKeyMsg processes all keyboard input.
// Returns (model, cmd, handled) where handled=true means the caller should return immediately.
func (m Model) handleKeyMsg(msg tea.KeyMsg, prevKeyTime time.Time) (Model, tea.Cmd, bool) {
	// Strip mouse tracking escape codes that may leak through as KeyMsg events.
	// These are SGR 1006 reports like ESC[<65;14;44M and should never be inserted
	// into the textarea.
	if msg.Type == tea.KeyRunes && len(msg.Runes) >= 8 {
		if isMouseEscapeSequence(string(msg.Runes)) {
			return m, nil, true
		}
	}

	// --- USAGE OVERLAY: intercept keys ---
	if m._usageOverlayActive {
		if msg.String() == "esc" || msg.String() == "q" {
			m._usageOverlayActive = false
			m._usageStats = nil
			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
			return m, nil, true
		}
		return m, nil, true
	}

	// --- SETUP FLOW: intercept keys ---
	if m._setupActive {
		return m.handleSetupKey(msg)
	}

	// --- MODEL PICKER: intercept keys ---
	if m._modelPickerActive {
		return m.handleModelPickerKey(msg)
	}

	// --- RACE MODEL PICKER: intercept keys ---
	if m._raceModelPickerActive {
		return m.handleRaceModelPickerKey(msg)
	}

	// --- COMMAND PICKER: intercept keys while open or activate on "/" ---
	if m._cmdPickerActive {
		return m.handleCommandPickerKey(msg)
	}
	if msg.Type == tea.KeyRunes && len(msg.Runes) > 0 {
		value := m.Textarea.Value()
		s := string(msg.Runes)
		if value == "" && s == "/" {
			m.activateCommandPicker("")
			return m, nil, true
		}
		if value == "/" && s != " " {
			m.activateCommandPicker(s)
			return m, nil, true
		}
		// Pasting a complete slash command (e.g. "/clear") activates the picker.
		if value == "" && strings.HasPrefix(s, "/") && !strings.Contains(s, " ") {
			m.activateCommandPicker(s[1:])
			return m, nil, true
		}
	}

	// --- RACE CONFIRM: intercept y/n ---
	if m._raceConfirmPending {
		if msg.String() == "y" || msg.String() == "Y" {
			m._raceConfirmPending = false
			m.RaceMode = true
			m._raceStartTime = time.Now()
			confirmLine := fmt.Sprintf("Cloning %d lanes...", len(m._raceConfirmModels))
			m.History = append(m.History, confirmLine)
			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
			m.Viewport.GotoBottom()
			task := m._raceConfirmTask
			models := m._raceConfirmModels
			srcDir := ""
			if m.Workspace != nil {
				srcDir = m.Workspace.Source
			}
			m._raceConfirmTask = ""
			m._raceConfirmModels = nil
			return m, startRaceCmd(task, models, srcDir), true
		}
		// Any other key cancels.
		m._raceConfirmPending = false
		m._raceConfirmTask = ""
		m._raceConfirmModels = nil
		m.History = append(m.History, "Race cancelled.")
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		m.Viewport.GotoBottom()
		return m, nil, true
	}

	// --- RACE COMPARING: interactive diff/merge selection ---
	if m.RaceMode && m._raceComparing && m.Race != nil {
		return m.handleRaceComparingKey(msg)
	}

	// --- RACE MODE: intercept q (abort) and x (kill lane) ---
	if m.RaceMode && m.Race != nil {
		switch msg.String() {
		case "q", "Q":
			_ = m.Race.Cleanup()
			m.History = append(m.History, "Race aborted.")
			m.RaceMode = false
			m.Race = nil
			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
			m.Viewport.GotoBottom()
			return m, nil, true
		case "x", "X":
			for _, l := range m.Race.Lanes {
				if l != nil && l.Status == race.LaneRunning {
					l.Kill()
					break
				}
			}
			return m, nil, true
		}
	}

	// --- APP-LEVEL HOTKEYS (work regardless of input focus) ---
	switch msg.String() {
	case "esc":
		if m.IsThinking {
			if m.SendFunc != nil {
				m.SendFunc(map[string]interface{}{
					"type": "interrupt",
				})
			}
			m._interruptPending = true
			m.Toast = "Interrupting..."
			m.ToastEnd = time.Now().Add(2 * time.Second)
		}
		return m, nil, true
	}

	// Handle confirmation gate shortcuts
	if m.ConfirmID != "" {
		return m.handleConfirmKey(msg)
	}

	switch msg.Type {
	case tea.KeyCtrlC:
		// Write metrics report on exit
		if m._metrics != nil {
			m._metrics.WriteReport("/tmp/kyrex_render_metrics.txt")
		}
		return m, tea.Quit, true
	case tea.KeyCtrlD:
		// Dump render metrics to file
		if m._metrics != nil {
			path := "/tmp/kyrex_render_metrics.txt"
			m._metrics.WriteReport(path)
			m.Toast = "Metrics → " + path
			m.ToastEnd = time.Now().Add(3 * time.Second)
		}
		return m, nil, true
	case tea.KeyCtrlB: // Toggle Sidebar
		m.ShowSidebar = !m.ShowSidebar
		return m, func() tea.Msg {
			return tea.WindowSizeMsg{Width: m.Width, Height: m.Height}
		}, true
	case tea.KeyCtrlY: // Copy last assistant response
		if len(m.History) > 0 {
			idx := len(m.History) - 1
			for idx >= 0 && (strings.HasPrefix(m.History[idx], "_Thinking:_") ||
				strings.HasPrefix(m.History[idx], "_Logs:_") ||
				strings.HasPrefix(m.History[idx], "> ")) {
				idx--
			}
			if idx >= 0 {
				clipboard.WriteAll(m.History[idx])
			} else {
				clipboard.WriteAll(m.History[len(m.History)-1])
			}
		}
		return m, nil, true
	case tea.KeyEnter, tea.KeyCtrlJ: // Submit on Enter or Ctrl+J
		return m.handleSubmit(msg, prevKeyTime)
	}

	return m, nil, false
}

func filterModels(all []string, filter string) []string {
	if filter == "" {
		return all
	}
	lower := strings.ToLower(filter)
	var out []string
	for _, item := range all {
		if strings.Contains(strings.ToLower(item), lower) {
			out = append(out, item)
		}
	}
	return out
}

func (m Model) handleModelPickerKey(msg tea.KeyMsg) (Model, tea.Cmd, bool) {
	switch msg.String() {
	case "esc":
		m._modelPickerActive = false
		m._modelPickerItems = nil
		m._modelPickerInput = ""
		m._modelPickerIndex = 0
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, nil, true
	case "up":
		if len(m._modelPickerItems) > 0 {
			m._modelPickerIndex--
			if m._modelPickerIndex < 0 {
				m._modelPickerIndex = len(m._modelPickerItems) - 1
			}
			m._modelPickerInput = ""
		}
	case "down":
		if len(m._modelPickerItems) > 0 {
			m._modelPickerIndex++
			if m._modelPickerIndex >= len(m._modelPickerItems) {
				m._modelPickerIndex = 0
			}
			m._modelPickerInput = ""
		}
	case "enter":
		idx := m._modelPickerIndex
		if m._modelPickerInput != "" {
			idx = 0
			fmt.Sscanf(m._modelPickerInput, "%d", &idx)
			idx--
		}
		if idx >= 0 && idx < len(m._modelPickerItems) {
			selected := m._modelPickerItems[idx]
			m._modelPickerActive = false
			m._modelPickerItems = nil
			m._modelPickerInput = ""
			m._modelPickerIndex = 0
			if m.SendFunc != nil {
				m.SendFunc(map[string]string{
					"type":    "command",
					"content": "/model " + selected,
				})
			}
			m.History = append(m.History, "> /model "+selected)
			m.Toast = "Model: " + selected
			m.ToastEnd = time.Now().Add(2 * time.Second)
			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
			return m, nil, true
		}
	case "backspace":
		if len(m._modelPickerFilter) > 0 {
			runes := []rune(m._modelPickerFilter)
			m._modelPickerFilter = string(runes[:len(runes)-1])
			m._modelPickerItems = filterModels(m._modelPickerAllItems, m._modelPickerFilter)
			m._modelPickerIndex = 0
		}
	default:
		if len(msg.String()) == 1 {
			m._modelPickerFilter += msg.String()
			m._modelPickerItems = filterModels(m._modelPickerAllItems, m._modelPickerFilter)
			m._modelPickerIndex = 0
		}
	}
	m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
	return m, nil, true
}

// handleRaceModelPickerKey handles keyboard input while the race model multi-select picker is open.
// Terminal-safe: matches on both tea.KeyType and msg.String() values for space/enter.
func (m Model) handleRaceModelPickerKey(msg tea.KeyMsg) (Model, tea.Cmd, bool) {
	// --- ESC: cancel picker AND wizard entirely ---
	if msg.Type == tea.KeyEsc || msg.String() == "esc" {
		m._raceModelPickerActive = false
		m._raceModelPickerLoading = false
		m._raceModelPickerAll = nil
		m._raceModelPickerItems = nil
		m._raceModelPickerFilter = ""
		m._raceModelPickerIndex = 0
		m._raceModelPickerSelected = nil
		m._raceWizardStep = 0
		m._raceWizardTask = ""
		m.History = append(m.History, "Race setup cancelled.")
		m.Textarea.Reset()
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		m.Viewport.GotoBottom()
		return m, nil, true
	}

	// --- UP ---
	if msg.Type == tea.KeyUp || msg.String() == "up" {
		if len(m._raceModelPickerItems) > 0 {
			m._raceModelPickerIndex--
			if m._raceModelPickerIndex < 0 {
				m._raceModelPickerIndex = len(m._raceModelPickerItems) - 1
			}
		}
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, nil, true
	}

	// --- DOWN ---
	if msg.Type == tea.KeyDown || msg.String() == "down" {
		if len(m._raceModelPickerItems) > 0 {
			m._raceModelPickerIndex++
			if m._raceModelPickerIndex >= len(m._raceModelPickerItems) {
				m._raceModelPickerIndex = 0
			}
		}
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, nil, true
	}

	// --- SPACE: toggle selection (max 4) ---
	s := string(msg.Runes)
	isSpace := msg.Type == tea.KeySpace || s == " "
	if isSpace {
		if len(m._raceModelPickerItems) == 0 {
			return m, nil, true
		}
		idx := m._raceModelPickerIndex
		if idx < 0 || idx >= len(m._raceModelPickerItems) {
			return m, nil, true
		}
		model := m._raceModelPickerItems[idx]
		// Check if already selected
		found := -1
		for i, sel := range m._raceModelPickerSelected {
			if sel == model {
				found = i
				break
			}
		}
		if found >= 0 {
			// Deselect
			m._raceModelPickerSelected = append(m._raceModelPickerSelected[:found], m._raceModelPickerSelected[found+1:]...)
		} else {
			if len(m._raceModelPickerSelected) >= 4 {
				m.Toast = "Max 4 models per race"
				m.ToastEnd = time.Now().Add(2 * time.Second)
			} else {
				m._raceModelPickerSelected = append(m._raceModelPickerSelected, model)
			}
		}
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, nil, true
	}

	// --- ENTER: confirm ---
	isEnter := msg.Type == tea.KeyEnter || s == "\r" || s == "\n" || msg.String() == "enter"
	if isEnter {
		var selected []string
		if len(m._raceModelPickerSelected) > 0 {
			selected = m._raceModelPickerSelected
		} else if len(m._raceModelPickerItems) > 0 {
			idx := m._raceModelPickerIndex
			if idx >= 0 && idx < len(m._raceModelPickerItems) {
				selected = []string{m._raceModelPickerItems[idx]}
			}
		}
		if len(selected) == 0 {
			return m, nil, true
		}
		if len(selected) > 4 {
			selected = selected[:4]
		}
		// Deactivate picker
		m._raceModelPickerActive = false
		m._raceModelPickerLoading = false
		m._raceModelPickerAll = nil
		m._raceModelPickerItems = nil
		m._raceModelPickerFilter = ""
		m._raceModelPickerIndex = 0
		m._raceModelPickerSelected = nil
		// Hand off to existing confirmation flow
		m._raceWizardStep = 0
		task := m._raceWizardTask
		m._raceWizardTask = ""
		m.History = append(m.History, fmt.Sprintf("Race: %d lanes ≈ %d× token cost. y to start, any other key cancels.", len(selected), len(selected)))
		m._raceConfirmPending = true
		m._raceConfirmTask = task
		m._raceConfirmModels = selected
		m.Textarea.Reset()
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		m.Viewport.GotoBottom()
		return m, nil, true
	}

	// --- BACKSPACE: edit filter ---
	if msg.Type == tea.KeyBackspace || msg.String() == "backspace" {
		if len(m._raceModelPickerFilter) > 0 {
			runes := []rune(m._raceModelPickerFilter)
			m._raceModelPickerFilter = string(runes[:len(runes)-1])
			m._raceModelPickerItems = filterModels(m._raceModelPickerAll, m._raceModelPickerFilter)
			m._raceModelPickerIndex = 0
		}
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, nil, true
	}

	// --- RUNES: type-to-filter ---
	if msg.Type == tea.KeyRunes && len(msg.Runes) > 0 {
		ch := string(msg.Runes)
		// Ignore space/enter here — handled above
		if ch == " " || ch == "\r" || ch == "\n" {
			return m, nil, true
		}
		m._raceModelPickerFilter += ch
		m._raceModelPickerItems = filterModels(m._raceModelPickerAll, m._raceModelPickerFilter)
		m._raceModelPickerIndex = 0
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, nil, true
	}

	return m, nil, false
}

func (m Model) handleConfirmKey(msg tea.KeyMsg) (Model, tea.Cmd, bool) {
	switch msg.String() {
	case "y", "Y":
		m = m.approveConfirm()
		return m, nil, true
	case "n", "N":
		if m.SendFunc != nil {
			m.SendFunc(map[string]interface{}{
				"type":     "confirm_response",
				"id":       m.ConfirmID,
				"approved": false,
			})
		}
		m.History = append(m.History, "\\U000f0159  Rejected change to: "+m.ConfirmPath)
		m.Timeline.UpdateByID(m.ConfirmID, components.StatusWarning, "Rejected — "+m.ConfirmPath)
		m.ConfirmID = ""
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, nil, true
	}
	return m, nil, false
}

// handleSetupKey handles keyboard input during the setup flow.
// Returns (model, cmd, handled) where handled=true means the caller should return immediately.
func (m Model) handleSetupKey(msg tea.KeyMsg) (Model, tea.Cmd, bool) {
	switch m._setupStep {
	case 0: // Provider picker
		return m.handleSetupProviderKey(msg)
	case 1: // API key input
		return m.handleSetupAPIKeyKey(msg)
	case 2: // Model picker
		return m.handleSetupModelKey(msg)
	case 3: // Connection test
		return m.handleSetupTestKey(msg)
	case 4: // Save confirmation
		return m.handleSetupSaveKey(msg)
	}
	return m, nil, false
}

func (m Model) handleSetupProviderKey(msg tea.KeyMsg) (Model, tea.Cmd, bool) {
	switch msg.String() {
	case "esc", "q":
		m._setupActive = false
		m._setupOllama = false
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, nil, true
	case "1", "2", "3", "4", "5", "6":
		providerMap := map[string]struct {
			provider string
			baseURL  string
		}{
			"1": {"openai", "https://opencode.ai/zen/go/v1"},
			"2": {"openai", "https://openrouter.ai/api/v1"},
			"3": {"openai", "https://api.openai.com/v1"},
			"4": {"anthropic", "https://api.anthropic.com"},
			"5": {"", ""}, // Custom - need more input
			"6": {"openai", "http://localhost:11434/v1"},
		}
		if p, ok := providerMap[msg.String()]; ok {
			m._setupProvider = p.provider
			m._setupBaseURL = p.baseURL
			if msg.String() == "6" {
				m._setupOllama = true
				m._setupAPIKey = "ollama"
				m._setupStep = 2
				m._setupInput = ""
				m._setupCursorPos = 0
				m._setupModels = nil
				m._setupError = ""
				m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
				return m, fetchModelsCmd(m._setupProvider, m._setupAPIKey, m._setupBaseURL), true
			}
			m._setupStep = 1
			m._setupInput = ""
			m._setupCursorPos = 0
		}
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, nil, true
	case "enter":
		if m._setupProvider != "" {
			if m._setupOllama {
				m._setupStep = 2
			} else {
				m._setupStep = 1
			}
			m._setupInput = ""
			m._setupCursorPos = 0
			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		}
		return m, nil, true
	}
	return m, nil, true
}

func (m Model) handleSetupAPIKeyKey(msg tea.KeyMsg) (Model, tea.Cmd, bool) {
	switch msg.String() {
	case "esc", "q":
		// Go back to provider selection
		m._setupStep = 0
		m._setupProvider = ""
		m._setupBaseURL = ""
		m._setupOllama = false
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, nil, true
	case "enter":
		input := m._setupInput
		if input == "" {
			return m, nil, true
		}
		// Check if it's an env var (ALL_CAPS)
		if len(input) > 0 && input == strings.ToUpper(input) && strings.ContainsAny(input, "ABCDEFGHIJKLMNOPQRSTUVWXYZ") {
			m._setupAPIKeyEnv = input
			m._setupAPIKey = ""
		} else {
			m._setupAPIKey = input
			m._setupAPIKeyEnv = ""
		}
		m._setupStep = 2
		m._setupInput = ""
		m._setupCursorPos = 0
		m._setupModels = nil
		m._setupError = ""
		// Fetch models in background
		return m, fetchModelsCmd(m._setupProvider, m._setupAPIKey, m._setupBaseURL), true
	case "backspace":
		if m._setupCursorPos > 0 {
			m._setupInput = m._setupInput[:m._setupCursorPos-1] + m._setupInput[m._setupCursorPos:]
			m._setupCursorPos--
		}
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, nil, true
	case "left":
		if m._setupCursorPos > 0 {
			m._setupCursorPos--
		}
		return m, nil, true
	case "right":
		if m._setupCursorPos < len(m._setupInput) {
			m._setupCursorPos++
		}
		return m, nil, true
	}
	if msg.Type == tea.KeyRunes {
		runes := msg.Runes
		m._setupInput = m._setupInput[:m._setupCursorPos] + string(runes) + m._setupInput[m._setupCursorPos:]
		m._setupCursorPos += len(runes)
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, nil, true
	}
	return m, nil, true
}

func (m Model) handleSetupModelKey(msg tea.KeyMsg) (Model, tea.Cmd, bool) {
	switch msg.String() {
	case "esc", "q":
		if m._setupCustomModel {
			// Cancel custom model input, go back to model list
			m._setupCustomModel = false
			m._setupModelFilter = ""
			m._setupFilteredModels = m._setupModels
			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		} else {
			m._setupActive = false
			m._setupOllama = false
			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		}
		return m, nil, true
	case "up":
		if !m._setupCustomModel && len(m._setupFilteredModels) > 0 && m._setupCursorPos > 0 {
			m._setupCursorPos--
		}
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, nil, true
	case "down":
		if !m._setupCustomModel && len(m._setupFilteredModels) > 0 && m._setupCursorPos < len(m._setupFilteredModels)-1 {
			m._setupCursorPos++
		}
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, nil, true
	case "enter":
		if m._setupCustomModel {
			// Save custom model name
			if m._setupInput != "" {
				m._setupModel = m._setupInput
				if m._setupOllama {
					m._setupStep = 4
				} else {
					m._setupStep = 3
				}
				m._setupTestResult = ""
				m._setupTestPassed = false
				m._setupCustomModel = false
				m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
			}
		} else if m._setupCursorPos >= 0 && m._setupCursorPos < len(m._setupFilteredModels) {
			m._setupModel = m._setupFilteredModels[m._setupCursorPos]
			if m._setupOllama {
				m._setupStep = 4
			} else {
				m._setupStep = 3
			}
			m._setupTestResult = ""
			m._setupTestPassed = false
			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		}
		return m, nil, true
	case "tab":
		// Switch to custom model input
		m._setupCustomModel = true
		m._setupInput = m._setupModel
		m._setupCursorPos = len(m._setupInput)
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, nil, true
	case "backspace":
		if m._setupCustomModel {
			if m._setupCursorPos > 0 {
				m._setupInput = m._setupInput[:m._setupCursorPos-1] + m._setupInput[m._setupCursorPos:]
				m._setupCursorPos--
			}
		} else if len(m._setupModelFilter) > 0 {
			m._setupModelFilter = m._setupModelFilter[:len(m._setupModelFilter)-1]
			m._setupCursorPos = 0
			// Filter models
			m.filterModels()
		}
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, nil, true
	case "left":
		if m._setupCustomModel && m._setupCursorPos > 0 {
			m._setupCursorPos--
		}
		return m, nil, true
	case "right":
		if m._setupCustomModel && m._setupCursorPos < len(m._setupInput) {
			m._setupCursorPos++
		}
		return m, nil, true
	}
	// Handle text input (letters, numbers, etc.)
	if msg.Type == tea.KeyRunes {
		runes := msg.Runes
		if m._setupCustomModel {
			// Custom model input
			m._setupInput = m._setupInput[:m._setupCursorPos] + string(runes) + m._setupInput[m._setupCursorPos:]
			m._setupCursorPos += len(runes)
		} else {
			// Filter input
			m._setupModelFilter += string(runes)
			m._setupCursorPos = 0
			// Filter models
			m.filterModels()
		}
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, nil, true
	}
	return m, nil, true
}

// filterModels filters the models list based on _setupModelFilter
func (m *Model) filterModels() {
	if m._setupModelFilter == "" {
		m._setupFilteredModels = m._setupModels
		return
	}
	
	filtered := []string{}
	filter := strings.ToLower(m._setupModelFilter)
	for _, model := range m._setupModels {
		if strings.Contains(strings.ToLower(model), filter) {
			filtered = append(filtered, model)
		}
	}
	m._setupFilteredModels = filtered
}

func (m Model) handleSetupTestKey(msg tea.KeyMsg) (Model, tea.Cmd, bool) {
	switch msg.String() {
	case "esc", "q":
		m._setupActive = false
		m._setupOllama = false
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, nil, true
	case "enter", "t":
		// Run connection test
		m._setupTestResult = "Testing connection..."
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, testConnectionCmd(m._setupProvider, m._setupAPIKey, m._setupBaseURL, m._setupModel), true
	case "s":
		// Skip test and go to save
		m._setupStep = 4
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, nil, true
	}
	return m, nil, true
}

func (m Model) handleSetupSaveKey(msg tea.KeyMsg) (Model, tea.Cmd, bool) {
	switch msg.String() {
	case "esc", "q":
		m._setupActive = false
		m._setupOllama = false
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, nil, true
	case "y", "Y":
		// Save configuration
		m._setupSaving = true
		err := saveConfig(m._setupProvider, m._setupBaseURL, m._setupAPIKey, m._setupAPIKeyEnv, m._setupModel, m._setupHeaders)
		if err != nil {
			m._setupError = "Failed to save: " + err.Error()
			m._setupSaving = false
		} else {
			m.Toast = "Configuration saved! Restart required."
			m._setupActive = false
			// Notify engine to reload config
			if m.SendFunc != nil {
				m.SendFunc(map[string]interface{}{
					"type":    "command",
					"content": "/model " + m._setupModel,
				})
			}
		}
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, nil, true
	case "n", "N":
		m._setupActive = false
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, nil, true
	}
	return m, nil, true
}

func (m Model) handleSubmit(msg tea.KeyMsg, prevKeyTime time.Time) (Model, tea.Cmd, bool) {
	// Paste burst detection: if Enter arrives < 25ms after the
	// previous keystroke, it's part of a paste — insert as a
	// literal newline instead of submitting.
	if msg.Type == tea.KeyEnter && time.Since(prevKeyTime) < 8*time.Millisecond {
		return m, nil, false
	}


	input := strings.TrimSpace(m.Textarea.Value())
	if input == "" {
		return m, nil, true
	}

	// ── Race Wizard: intercept submissions while wizard is active ──
	if m._raceWizardStep > 0 {
		// Cancellation
		if input == "q" || input == "/cancel" {
			m._raceWizardStep = 0
			m._raceWizardTask = ""
			m.History = append(m.History, "> "+input)
			m.History = append(m.History, "Race setup cancelled.")
			m.Textarea.Reset()
			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
			m.Viewport.GotoBottom()
			return m, nil, true
		}

		if m._raceWizardStep == 1 {
			// Step 1: awaiting task description
			task := strings.TrimSpace(input)
			if task == "" {
				m.History = append(m.History, "> "+input)
				m.History = append(m.History, "Task cannot be empty — describe what the models should do:")
				m.Textarea.Reset()
				m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
				m.Viewport.GotoBottom()
				return m, nil, true
			}
			m._raceWizardTask = task
			m._raceWizardStep = 2
			m.History = append(m.History, "> "+task)
			// Try fetching models from workspace config for the picker
			prov, ak, bu := loadWorkspaceConfig(&m)
			if prov != "" {
				m._raceModelPickerLoading = true
				m.History = append(m.History, "Fetching models…")
				m.Textarea.Reset()
				m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
				m.Viewport.GotoBottom()
				return m, fetchModelsCmd(prov, ak, bu), true
			}
			m.History = append(m.History, "Models? (comma-separated, max 4 — e.g. deepseek/deepseek-v4-flash,tencent/hy3-preview)")
			m.Textarea.Reset()
			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
			m.Viewport.GotoBottom()
			return m, nil, true
		}

		if m._raceWizardStep == 2 {
			// Step 2: awaiting model list
			modelsStr := strings.TrimSpace(input)
			var models []string
			if modelsStr != "" {
				raw := strings.Split(modelsStr, ",")
				for _, mName := range raw {
					mName = strings.TrimSpace(mName)
					if mName != "" {
						models = append(models, mName)
					}
				}
			}
			if len(models) == 0 {
				m.History = append(m.History, "> "+input)
				m.History = append(m.History, "Models list cannot be empty. Try again (comma-separated, max 4):")
				m.Textarea.Reset()
				m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
				m.Viewport.GotoBottom()
				return m, nil, true
			}
			if len(models) > 4 {
				m.History = append(m.History, "> "+input)
				m.History = append(m.History, fmt.Sprintf("Max 4 models per race (got %d). Try again:", len(models)))
				m.Textarea.Reset()
				m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
				m.Viewport.GotoBottom()
				return m, nil, true
			}

			// Exit wizard and hand off to existing confirmation flow
			m._raceWizardStep = 0
			task := m._raceWizardTask
			m._raceWizardTask = ""
			m.History = append(m.History, "> "+modelsStr)
			m.History = append(m.History, fmt.Sprintf("Race: %d lanes ≈ %d× token cost. y to start, any other key cancels.", len(models), len(models)))
			m._raceConfirmPending = true
			m._raceConfirmTask = task
			m._raceConfirmModels = models
			m.Textarea.Reset()
			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
			m.Viewport.GotoBottom()
			return m, nil, true
		}
	}

	if input == "/autoapprove" || strings.HasPrefix(input, "/autoapprove ") {
		args := strings.TrimSpace(strings.TrimPrefix(input, "/autoapprove"))
		switch {
		case args == "off":
			m.AutoApprove = false
			m.Toast = "Auto-approve: off"
		case args == "":
			m.AutoApprove = !m.AutoApprove
			if m.AutoApprove {
				if m.AutoApproveDelay == 0 {
					m.AutoApproveDelay = 5 * time.Second
				}
				m.Toast = fmt.Sprintf("Auto-approve: on (%ds delay)", int(m.AutoApproveDelay.Seconds()))
			} else {
				m.Toast = "Auto-approve: off"
			}
		default:
			seconds, err := strconv.Atoi(args)
			if err != nil || seconds < 1 {
				m.Toast = "Usage: /autoapprove, /autoapprove <seconds>, or /autoapprove off"
			} else {
				m.AutoApprove = true
				m.AutoApproveDelay = time.Duration(seconds) * time.Second
				m.Toast = fmt.Sprintf("Auto-approve: on (%ds delay)", seconds)
			}
		}
		m.ToastEnd = time.Now().Add(3 * time.Second)
		m.Textarea.Reset()
		return m, nil, true
	}

	// Commit any pending reasoning to history before starting new turn
	if m.Reasoning != "" {
		m.History = append(m.History, "_Thinking:_\n"+m.Reasoning)
		m.Reasoning = ""
	}
	// Also commit any live answer from previous turn
	if m.CurrToken != "" {
		m.History = append(m.History, "_Overview:_\n"+m.CurrToken)
		m.CurrToken = ""
	}

	// Clear viewport cache and refresh to show old content moved up
	m._cachedViewportContent = ""
	m._viewportDirty = true
	m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
	m.Viewport.GotoBottom()

	m.Textarea.Reset()

	// Handle /new locally — clear conversation history and reset state
	if input == "/new" {
		m.History = nil
		m.Turns = nil
		m.CurrentTurn = nil
		m.CurrToken = ""
		m.Reasoning = ""
		m.CurrentTool = ""
		m.ToolArgs = ""
		m.ToolResult = ""
		m.Phase = PhaseIdle
		m.IsThinking = false
		m.IsSending = false
		m._interruptPending = false
		m._suppressEngine = false
		m._cachedViewportContent = ""
		m._viewportDirty = true
		m._stableHistoryContent = ""
		m._stableHistoryLen = 0
		m._historyCacheValid = false
		m._timerActive = false
		m.Timer = 0
		m.ExecTree = NewExecutionTree()
		m.Timeline.Clear()
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		m.Viewport.GotoBottom()
		m.Toast = "Conversation cleared"
		m.ToastEnd = time.Now().Add(2 * time.Second)
		return m, nil, true
	}

	// ── /race: start a parallel race across models ──
	if input == "/race" || strings.HasPrefix(input, "/race ") {
		if m.RaceMode {
			m.History = append(m.History, "> "+input)
			m.History = append(m.History, "A race is already in progress. Press q to abort first.")
			m.Textarea.Reset()
			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
			m.Viewport.GotoBottom()
			return m, nil, true
		}

		// Enter wizard if --models flag is absent
		rest := strings.TrimSpace(strings.TrimPrefix(input, "/race"))
		if rest == "" {
			// Bare /race → wizard step 1
			m.History = append(m.History, "> "+input)
			m._raceWizardStep = 1
			m._raceWizardTask = ""
			m.History = append(m.History, "Race task? (describe what the models should do)")
			m.Textarea.Reset()
			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
			m.Viewport.GotoBottom()
			return m, nil, true
		}
		if !strings.Contains(rest, "--models") {
			// /race <task> without --models → wizard step 2 with task pre-filled
			m.History = append(m.History, "> "+input)
			m._raceWizardStep = 2
			m._raceWizardTask = rest
			// Try fetching models from workspace config for the picker
			prov, ak, bu := loadWorkspaceConfig(&m)
			if prov != "" {
				m._raceModelPickerLoading = true
				m.History = append(m.History, "Fetching models…")
				m.Textarea.Reset()
				m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
				m.Viewport.GotoBottom()
				return m, fetchModelsCmd(prov, ak, bu), true
			}
			m.History = append(m.History, "Models? (comma-separated, max 4 — e.g. deepseek/deepseek-v4-flash,tencent/hy3-preview)")
			m.Textarea.Reset()
			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
			m.Viewport.GotoBottom()
			return m, nil, true
		}

		// Full one-line form with --models flag
		task, models, err := parseRaceCommand(input)
		if err != nil {
			m.History = append(m.History, "> "+input)
			m.History = append(m.History, "Usage: /race <task text> --models <model1>,<model2>[,...]")
			m.History = append(m.History, "Max 4 models. Example: /race Fix the bug --models gpt-4,claude-3-5-sonnet")
			m.Textarea.Reset()
			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
			m.Viewport.GotoBottom()
			return m, nil, true
		}
		m.History = append(m.History, "> "+input)
		m.History = append(m.History, fmt.Sprintf("Race: %d lanes ≈ %d× token cost. y to start, any other key cancels.", len(models), len(models)))
		m._raceConfirmPending = true
		m._raceConfirmTask = task
		m._raceConfirmModels = models
		m.Textarea.Reset()
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		m.Viewport.GotoBottom()
		return m, nil, true
	}

	// ── /model: open model picker inline ──
		if input == "/model" || strings.HasPrefix(input, "/model ") {
			m.History = append(m.History, "> "+input)
			m._cachedViewportContent = ""
			m._viewportDirty = true
			m._modelPickerActive = true
			m._modelPickerLoading = true
			m._modelPickerItems = nil
			m._modelPickerCurrent = ""
			m._modelPickerIndex = 0
			m._modelPickerInput = ""
			// Trigger a fetchModels command so the picker populates
			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
			m.Viewport.GotoBottom()
			// Return early — don't send to engine; the picker handles the selection
			provider := m.getProvider()
			apiKey := m.getAPIKey()
			baseURL := m.getBaseURL()
			fmt.Fprintf(os.Stderr, "[DEBUG] /model command: provider=%s, baseURL=%s, apiKey_present=%v\n", provider, baseURL, apiKey != "")
			return m, fetchModelsCmd(provider, apiKey, baseURL), true
		}

	// Mobile-friendly toggle commands
	if input == ":sidebar" || input == ":w" {
		m.ShowSidebar = !m.ShowSidebar
		return m, func() tea.Msg {
			return tea.WindowSizeMsg{Width: m.Width, Height: m.Height}
		}, true
	}

	// Handle /setup command - open inline setup flow
	if input == "/setup" {
		m.History = append(m.History, "> /setup")
		m._cachedViewportContent = ""
		m._viewportDirty = true
		m._setupActive = true
		m._setupStep = 0
		m._setupOllama = false
		m._setupProvider = ""
		m._setupBaseURL = ""
		m._setupAPIKey = ""
		m._setupAPIKeyEnv = ""
		m._setupModel = ""
		m._setupModels = nil
		m._setupHeaders = ""
		m._setupTestResult = ""
		m._setupTestPassed = false
		m._setupSaving = false
		m._setupError = ""
		m._setupInput = ""
		m._setupCursorPos = 0
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		m.Viewport.GotoBottom()
		return m, nil, true
	}

	// Start session timer on first prompt
	if !m._timerActive {
		m._timerActive = true
		m.Timer = 0
	}

	// Re-enable engine messages for the new request
	m._suppressEngine = false
	if m._interruptPending {
		if !m.IsThinking {
			// Engine already done — safe to clear
			m.IsSending = false
			m._interruptPending = false
		} else {
			m.Toast = "Waiting for engine to cancel..."
			m.ToastEnd = time.Now().Add(2 * time.Second)
			return m, nil, true
		}
	}
	if m.SendFunc != nil {
		msgType := "chat"
		if strings.HasPrefix(input, "/") {
			msgType = "command"
		}
		m.SendFunc(map[string]string{
			"type":    msgType,
			"content": input,
		})
	}
	m.IsSending = true
	m._sendingTick = 0
	m.History = append(m.History, "> "+input)
	m.resetTurnState()
	m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
	m.Viewport.GotoBottom()

	return m, sendingTickCmd(), true
}


// approveConfirm performs the same approve+merge logic used by both the
// manual "y" keypress and the auto-approve timer.
func (m Model) approveConfirm() Model {
	if m.SendFunc != nil {
		m.SendFunc(map[string]interface{}{
			"type":     "confirm_response",
			"id":       m.ConfirmID,
			"approved": true,
		})
	}
	time.Sleep(150 * time.Millisecond)
	if m.Workspace != nil && m.Workspace.Root != m.Workspace.Source {
		if mergeErr := m.WorkspaceMgr.MergeFile(m.Workspace, m.ConfirmPath); mergeErr != nil {
			m.History = append(m.History, "⚠  Merge failed: "+mergeErr.Error())
		} else {
			fileName := filepath.Base(m.ConfirmPath)
			m.History = append(m.History, "\U000f012c  Approved \u2192 merged "+fileName+" into project")
		}
	} else {
		m.History = append(m.History, "\U000f012c  Approved change to: "+m.ConfirmPath)
	}
	m.Timeline.UpdateByID(m.ConfirmID, components.StatusSuccess, "Approved — "+m.ConfirmPath)
	m.ConfirmID = ""
	m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
	return m
}

// handleRaceComparingKey handles keys during the race comparing/merge phase.
// When viewing a diff: Esc or number returns to table, arrows/PgUp/PgDown scroll.
// When on the table: Up/Down highlight, number views diff, m merges, d/q discard.
func (m Model) handleRaceComparingKey(msg tea.KeyMsg) (Model, tea.Cmd, bool) {
	msgStr := msg.String()

	// ── Diff viewing mode ────────────────────────────────────────────
	if m._raceViewingDiff >= 0 {
		switch {
		case msgStr == "esc" || msgStr == "Escape":
			m._raceViewingDiff = -1
			m._raceDiffScroll = 0
			return m, nil, true
		case msgStr == "up":
			if m._raceDiffScroll > 0 {
				m._raceDiffScroll--
			}
			return m, nil, true
		case msgStr == "down":
			m._raceDiffScroll++
			return m, nil, true
		case msgStr == "pgup":
			m._raceDiffScroll -= m.Height / 2
			if m._raceDiffScroll < 0 {
				m._raceDiffScroll = 0
			}
			return m, nil, true
		case msgStr == "pgdown":
			m._raceDiffScroll += m.Height / 2
			return m, nil, true
		case msgStr == "home":
			m._raceDiffScroll = 0
			return m, nil, true
		case msgStr == "end":
			m._raceDiffScroll = 1 << 30 // large value, clamped in render
			return m, nil, true
		}

		// Number key matching current viewing lane returns to table
		if msg.Type == tea.KeyRunes && len(msg.Runes) > 0 {
			if n, err := strconv.Atoi(msgStr); err == nil && n == m._raceViewingDiff {
				m._raceViewingDiff = -1
				m._raceDiffScroll = 0
				return m, nil, true
			}
		}

		// d/q still work from diff view
		if msgStr == "d" || msgStr == "D" || msgStr == "q" || msgStr == "Q" {
			m.History = append(m.History, "Race discarded.")
			m = m.discardRace()
			return m, nil, true
		}

		// m works from diff view too
		if msgStr == "m" || msgStr == "M" {
			var mergeCmd tea.Cmd
			m, mergeCmd = m.executeMerge()
			return m, mergeCmd, true
		}

		return m, nil, true
	}

	// ── Table mode ───────────────────────────────────────────────────

	// Count lanes for dynamic range
	nLanes := len(m.Race.Lanes)

	switch {
	case msgStr == "esc" || msgStr == "Escape":
		// In comparing state, esc does nothing on the table (use d/q to exit)
		return m, nil, true

	case msgStr == "up":
		// Move highlight up, skipping non-mergeable lanes
		for i := m._raceHighlight - 1; i >= 0; i-- {
			if m.Race.Lanes[i] != nil && m.Race.Lanes[i].Status == race.LaneDone {
				m._raceHighlight = i
				break
			}
		}
		return m, nil, true

	case msgStr == "down":
		// Move highlight down, skipping non-mergeable lanes
		for i := m._raceHighlight + 1; i < nLanes; i++ {
			if m.Race.Lanes[i] != nil && m.Race.Lanes[i].Status == race.LaneDone {
				m._raceHighlight = i
				break
			}
		}
		return m, nil, true

	case msgStr == "d" || msgStr == "D" || msgStr == "q" || msgStr == "Q":
		m.History = append(m.History, "Race discarded.")
		m = m.discardRace()
		return m, nil, true

	case msgStr == "m" || msgStr == "M":
		if m._raceMergePending {
			// Already merging
			return m, nil, true
		}
		// executeMerge returns (Model, tea.Cmd) — add true for handled
		var mergeCmd tea.Cmd
		m, mergeCmd = m.executeMerge()
		return m, mergeCmd, true
	}

	// Number key: toggle diff view for that lane
	if msg.Type == tea.KeyRunes && len(msg.Runes) > 0 {
		if n, err := strconv.Atoi(msgStr); err == nil && n >= 1 && n <= nLanes {
			laneIdx := n - 1
			if m.Race.Lanes[laneIdx] != nil && m.Race.Lanes[laneIdx].Status == race.LaneDone {
				if m._raceViewingDiff == laneIdx {
					m._raceViewingDiff = -1
					m._raceDiffScroll = 0
				} else {
					m._raceViewingDiff = laneIdx
					m._raceDiffScroll = 0
				}
			}
			return m, nil, true
		}
	}

	return m, nil, true
}

// executeMerge starts a merge for the currently highlighted lane.
// Returns a cmd that will produce a RaceMergeResultMsg.
func (m Model) executeMerge() (Model, tea.Cmd) {
	idx := m._raceHighlight
	if idx < 0 || idx >= len(m.Race.Lanes) {
		m.History = append(m.History, "No lane selected for merge.")
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, nil
	}
	l := m.Race.Lanes[idx]
	if l == nil {
		m.History = append(m.History, "Selected lane is nil.")
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, nil
	}
	if l.Status != race.LaneDone {
		m.History = append(m.History, fmt.Sprintf("Lane %d (%s) status is %s — must be 'done' to merge.", l.ID, l.Model, l.Status))
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, nil
	}

	sourceDir := ""
	if m.Workspace != nil {
		sourceDir = m.Workspace.Source
	}
	if sourceDir == "" {
		m.History = append(m.History, "No workspace source directory — cannot determine merge target.")
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, nil
	}
	if m.WorkspaceMgr == nil {
		m.History = append(m.History, "Workspace manager not available — cannot merge.")
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, nil
	}

	m._raceMergePending = true
	m.History = append(m.History, fmt.Sprintf("Merging Lane %d (%s)...", l.ID, l.Model))
	m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
	return m, mergeLaneCmd(m.Race, idx, sourceDir, m.WorkspaceMgr)
}
