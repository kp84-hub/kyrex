package tui

import (
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/atotto/clipboard"
	tea "github.com/charmbracelet/bubbletea"
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
	"/export", "/skill", "/spawn", "/mcp", "/model", "/help", "/setup",
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
		m.Textarea.SetValue(m._cmdPickerItems[m._cmdPickerIndex])
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
		if s == " " {
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

func (m Model) handleConfirmKey(msg tea.KeyMsg) (Model, tea.Cmd, bool) {
	switch msg.String() {
	case "y", "Y":
		// Rift: merge workspace changes into the source project on approve
		if m.Workspace != nil && m.Workspace.Root != m.Workspace.Source {
			changes, mergeErr := m.WorkspaceMgr.MergeBack(m.Workspace)
			if mergeErr != nil {
				m.History = append(m.History, "⚠  Merge failed: "+mergeErr.Error())
			} else if len(changes) > 0 {
				m.History = append(m.History, fmt.Sprintf("\\U000f012c  Merged %d files into project", len(changes)))
			}
			m.WorkspaceMgr.Discard(m.Workspace)
			m.Workspace = nil
		}
		if m.SendFunc != nil {
			m.SendFunc(map[string]interface{}{
				"type":     "confirm_response",
				"id":       m.ConfirmID,
				"approved": true,
			})
		}
		m.History = append(m.History, "\\U000f012c  Approved change to: "+m.ConfirmPath)
		m.Timeline.UpdateByID(m.ConfirmID, components.StatusSuccess, "Approved — "+m.ConfirmPath)
		m.ConfirmID = ""
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, nil, true
	case "n", "N":
		// Rift: discard workspace changes on reject
		if m.Workspace != nil && m.Workspace.Root != m.Workspace.Source {
			m.WorkspaceMgr.Discard(m.Workspace)
			m.Workspace = nil
		}
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
		// Let it fall through to the textarea for InsertNewline
		return m, nil, false
	}

	input := strings.TrimSpace(m.Textarea.Value())
	if input == "" {
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

