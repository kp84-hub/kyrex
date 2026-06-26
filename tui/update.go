package tui

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
)

// MsgFromEngine is the IPC message type received from the Python engine process.
type MsgFromEngine struct {
	Type          string
	ID            string
	Content       string
	Phase         Phase
	Name          string
	Args          interface{}
	Result        interface{}
	Value         string
	Model         string
	Provider      string
	Context       string
	Files         interface{}
	Stdout        string
	Reasoning     string
	RequestID     string
	Path          string
	Diff          string
	Todos         []string
	SessionBranch string
}

// SetupFetchModelsMsg is sent to fetch models from provider.
type SetupFetchModelsMsg struct {
	Provider string
	APIKey   string
	BaseURL  string
}

// SetupModelsFetchedMsg is sent when models are fetched.
type SetupModelsFetchedMsg struct {
	Models []string
	Error  string
}

// SetupTestConnectionMsg is sent to test connection.
type SetupTestConnectionMsg struct {
	Provider string
	APIKey   string
	BaseURL  string
	Model    string
}

// SetupTestResultMsg is sent with connection test results.
type SetupTestResultMsg struct {
	Passed bool
	Result string
}

// SetupSaveConfigMsg is sent to save configuration.
type SetupSaveConfigMsg struct {
	Provider  string
	BaseURL   string
	APIKey    string
	APIKeyEnv string
	Model     string
	Headers   string
}

// SetupSaveResultMsg is sent with save results.
type SetupSaveResultMsg struct {
	Success bool
	Error   string
}

type TickMsg time.Time

func Tick() tea.Cmd {
	return tea.Tick(time.Second, func(t time.Time) tea.Msg {
		return TickMsg(t)
	})
}

type FastTickMsg time.Time

func FastTick() tea.Cmd {
	return tea.Tick(300*time.Millisecond, func(t time.Time) tea.Msg {
		return FastTickMsg(t)
	})
}

// TokenCoalesceMsg fires 16ms after the first token in a burst.
type TokenCoalesceMsg time.Time

func tokenCoalesceCmd() tea.Cmd {
	return tea.Tick(16*time.Millisecond, func(t time.Time) tea.Msg {
		return TokenCoalesceMsg(t)
	})
}

// SendingTickMsg fires every 350ms while IsSending is true to animate the dot counter.
type SendingTickMsg time.Time

func sendingTickCmd() tea.Cmd {
	return tea.Tick(350*time.Millisecond, func(t time.Time) tea.Msg {
		return SendingTickMsg(t)
	})
}

// TypewriterTickMsg fires every 30ms (or 45ms in final round) to advance the typewriter animation.
type TypewriterTickMsg time.Time

func typewriterTickCmd(finalRound bool) tea.Cmd {
	interval := 30 * time.Millisecond
	if finalRound {
		interval = 45 * time.Millisecond
	}
	return tea.Tick(interval, func(t time.Time) tea.Msg {
		return TypewriterTickMsg(t)
	})
}

func (m Model) Init() tea.Cmd {
	return tea.Batch(Tick(), FastTick())
}

// flushViewport rebuilds viewport content if it changed and only follows the
// bottom when the viewport was already anchored there.
func (m *Model) flushViewport() {
	if !m._viewportDirty {
		return
	}
	newContent := m.FullViewportContent(m.Viewport.Width)
	if newContent == m._lastSetContent {
		m._viewportDirty = false
		return
	}
	wasAtBottom := m.Viewport.AtBottom()
	m.Viewport.SetContent(newContent)
	m._lastSetContent = newContent

	// Use a dedicated persistent flag (never reset per-turn) to detect the
	// first real content render. _lastSetContent is unreliable because
	// resetTurnState() zeros it on every send.
	if !m._hasShownContent {
		m.Viewport.GotoBottom()

	} else if !m.ScrollLock && wasAtBottom {
		m.Viewport.GotoBottom()
	}

	m._viewportDirty = false
}

// Update is the main Bubble Tea update dispatcher.
func (m Model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	var (
		tiCmd tea.Cmd
		vpCmd tea.Cmd
		cmds  []tea.Cmd
		cmd    tea.Cmd
		handled bool
	)

	msgType := classifyMsg(msg)
	prevDirty := m._viewportDirty

	switch msg := msg.(type) {
	case tea.KeyMsg:
		prevKeyTime := m._lastKeyTime
		m._lastKeyTime = time.Now()

		m, cmd, handled = m.handleKeyMsg(msg, prevKeyTime)
		if handled {
			return m, cmd
		}

	case tea.MouseMsg:
		m, cmd, handled = m.handleMouseMsg(msg)
		if handled {
			return m, cmd
		}

	case tea.WindowSizeMsg:
		if m.Width == 0 && msg.Width < 120 {
			m.ShowSidebar = false
		}
		m.Width = msg.Width
		m.Height = msg.Height
		layout := m.recalculateLayout()
		m.applyLayout(layout)

			case FastTickMsg:
			if m.Reasoning != "" || m.CurrToken != "" || m.IsThinking {
				throttle := 150 * time.Millisecond
				if m.Reasoning != "" || m.CurrToken != "" {
					throttle = 50 * time.Millisecond
				}
				if m._viewportDirty && !m._tokenCoalescePending && time.Since(m._lastViewportFlush) > throttle {
					m.flushViewport()
					m._lastViewportFlush = time.Now()
				}
			}
			if m.Selecting && m.AutoScrollDir != 0 {
				// Smooth auto-scroll during selection drag
				scrollAmount := 2
				if m.AutoScrollDir > 0 {
					m.Viewport.LineDown(scrollAmount)
				} else {
					m.Viewport.LineUp(scrollAmount)
				}
				m._viewportDirty = true
				// Force viewport content refresh during scroll
				m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
			}
			cmds = append(cmds, FastTick())

		case TickMsg:
			if m.IsThinking {
				m.Timer++
			}
			if m.Reasoning != "" || m.CurrToken != "" || m.IsThinking {
				throttle := 150 * time.Millisecond
				if m.Reasoning != "" || m.CurrToken != "" {
					throttle = 50 * time.Millisecond
				}
				if m._viewportDirty && time.Since(m._lastViewportFlush) > throttle {
					m.flushViewport()
					m._lastViewportFlush = time.Now()
				}
			}
			if m.Toast != "" && time.Now().After(m.ToastEnd) {
				m.Toast = ""
				m._viewportDirty = true
			}

			// ── NEW: Check if chat_done delay has elapsed ──
			if m._chatDoneDelayActive && time.Now().After(m._chatDoneDelayEnd) {
				m = m.commitChatDoneImmediately()
			}

			cmds = append(cmds, Tick())

	case TokenCoalesceMsg:
		m._tokenCoalescePending = false
		m.flushViewport()
		m._lastViewportFlush = time.Now()

	case SendingTickMsg:
		if m.IsSending {
			m._sendingTick = (m._sendingTick + 1) % 3
			m._viewportDirty = true
			cmds = append(cmds, sendingTickCmd())
		}

			case TypewriterTickMsg:
			backlog := len(m.CurrToken) - m._typewriterPos
			if backlog > 300 {
				m._typewriterPos += backlog / 10
			} else {
				m._typewriterPos++
			}
			if m._typewriterPos >= len(m.CurrToken) {
				m._typewriterPos = len(m.CurrToken)
				m._typewriterPending = false
			} else {
				cmds = append(cmds, typewriterTickCmd(m._inFinalRound))
			}
			m._viewportDirty = true

	case MsgFromEngine:
		var engCmd tea.Cmd
		m, engCmd, _ = m.handleEngineMsg(msg)
		if engCmd != nil {
		cmds = append(cmds, engCmd)
		}

		case SetupFetchModelsMsg:
		// Start fetching models
			if m._setupActive && m._setupStep == 2 {
					cmds = append(cmds, fetchModelsCmd(msg.Provider, msg.APIKey, msg.BaseURL))
				}

	case SetupModelsFetchedMsg:
		// Models fetched — handle both setup wizard and standalone /model picker
		fmt.Fprintf(os.Stderr, "[DEBUG] SetupModelsFetchedMsg: error=%s, models=%d, _modelPickerActive=%v, _setupActive=%v\n", msg.Error, len(msg.Models), m._modelPickerActive, m._setupActive)
			if m._modelPickerActive {
				if msg.Error != "" {
					m._modelPickerItems = nil
					m._modelPickerLoading = false
					m._modelPickerActive = false
					m.Toast = "Model fetch failed: " + msg.Error
					m.ToastEnd = time.Now().Add(4 * time.Second)
				} else {
						m._modelPickerAllItems = msg.Models
						m._modelPickerItems = msg.Models
						m._modelPickerFilter = ""
						m._modelPickerLoading = false
						m._modelPickerIndex = 0
						m._modelPickerInput = ""
						m.Toast = fmt.Sprintf("Loaded %d models", len(msg.Models))
						m.ToastEnd = time.Now().Add(2 * time.Second)
					}
					m._viewportDirty = true
					m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
					return m, nil
				}
			if m._setupActive && m._setupStep == 2 {
					if msg.Error != "" {
						m._setupError = "Failed to fetch models: " + msg.Error
						m._setupModels = nil
						m._setupFilteredModels = nil
					} else {
						m._setupModels = msg.Models
						m._setupFilteredModels = msg.Models
						m._setupCursorPos = 0
						m._setupError = ""
					}
					m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
				}

	case SetupTestConnectionMsg:
		// Start testing connection
		if m._setupActive && m._setupStep == 3 {
			cmds = append(cmds, testConnectionCmd(msg.Provider, msg.APIKey, msg.BaseURL, msg.Model))
		}

	case SetupTestResultMsg:
		// Connection test result
		if m._setupActive && m._setupStep == 3 {
			m._setupTestPassed = msg.Passed
			m._setupTestResult = msg.Result
			if msg.Passed {
				// Auto-advance to save step when connection passes
				m._setupStep = 4
			}
			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		}

	case SetupSaveConfigMsg:
		// Start saving config
		if m._setupActive && m._setupStep == 4 {
			cmds = append(cmds, saveConfigCmd(msg.Provider, msg.BaseURL, msg.APIKey, msg.APIKeyEnv, msg.Model, msg.Headers))
		}

	case SetupSaveResultMsg:
		// Save result
		if m._setupActive && m._setupStep == 4 {
			m._setupSaving = false
			if msg.Success {
				m.Toast = "Configuration saved! Restarting engine..."
				m._setupActive = false
				// Send reload signal to engine
				if m.SendFunc != nil {
					m.SendFunc(map[string]interface{}{
						"type":    "command",
						"content": "/model " + m._setupModel,
					})
				}
			} else {
				m._setupError = "Failed to save: " + msg.Error
			}
			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		}
	}

	// Only pass keyboard messages to textarea
	switch msg.(type) {
	case tea.KeyMsg:
		m.Textarea, tiCmd = m.Textarea.Update(msg)
	default:
		tiCmd = nil
	}

	// Only pass messages to viewport that it actually needs to handle
	shouldUpdateViewport := false
	switch msg.(type) {
	case tea.MouseMsg:
		shouldUpdateViewport = true
	case tea.WindowSizeMsg:
		shouldUpdateViewport = true
	case tea.KeyMsg:
		keyMsg := msg.(tea.KeyMsg)
		switch keyMsg.Type {
		case tea.KeyPgUp, tea.KeyPgDown, tea.KeyHome, tea.KeyEnd,
			tea.KeyUp, tea.KeyDown, tea.KeyLeft, tea.KeyRight:
			shouldUpdateViewport = true
		}
	}

	if shouldUpdateViewport {
		m.Viewport, vpCmd = m.Viewport.Update(msg)
		cmds = append(cmds, vpCmd)
	}
	cmds = append(cmds, tiCmd)

	if m.Viewport.AtBottom() {
		m.ScrollLock = false
	} else if _, ok := msg.(tea.KeyMsg); ok {
		m.ScrollLock = true
	}

	if m._metrics != nil {
		causedDirty := !prevDirty && m._viewportDirty
		m._metrics.RecordMsg(msgType, causedDirty)
	}

	return m, tea.Batch(cmds...)
}

// classifyMsg returns a human-readable category for Bubble Tea messages.
func classifyMsg(msg tea.Msg) string {
	switch msg.(type) {
	case tea.KeyMsg:
		return "keypress"
	case tea.MouseMsg:
		return "mouse"
	case tea.WindowSizeMsg:
		return "resize"
	case FastTickMsg:
		return "fast_tick"
	case TickMsg:
		return "tick"
	case MsgFromEngine:
		engineMsg := msg.(MsgFromEngine)
		switch engineMsg.Type {
		case "token", "content":
			return "engine_token"
		case "reasoning":
			return "engine_reasoning"
		case "tool_start":
			return "engine_tool_start"
		case "tool_result":
			return "engine_tool_result"
		case "diff":
			return "engine_diff"
		case "phase":
			return "engine_phase"
		case "chat_done":
			return "engine_chat_done"
		case "log":
			return "engine_log"
		case "error":
			return "engine_error"
		case "session_state":
			return "engine_session_state"
		case "confirm_request":
			return "engine_confirm"
		case "tui_pause":
			return "engine_pause"
		default:
			return "engine_" + engineMsg.Type
		}
	default:
		return fmt.Sprintf("%T", msg)
	}
}

// resetTurnState clears all per-turn telemetry before starting a new request.
func (m *Model) resetTurnState() {
	m._turnHasTools = false
	m._inFinalRound = false
	m._typewriterPos = 0
	m._typewriterPending = false
	m._interruptPending = false
	m._resetTokenOnNextRound = false // safety: clear any stale round-boundary flag
	m.CurrToken = ""
	m.Reasoning = ""
	m.IsThinking = false
	m.Timer = 0
	m.ScrollLock = false
	m.Timeline.Clear()
	m.MissionSummary = ""
	m.ExecTree.Clear()
	m.Tools = NewToolTelemetry(50)
	m.CurrentTool = ""
	m.ToolArgs = ""
	m.ToolResult = ""
	m.ConfirmID = ""
	m.ConfirmPath = ""
	m.ConfirmDiff = ""
	m._phasePlanID = ""
	m._phaseExecID = ""
	m._lastToolID = ""
	m._cachedViewportContent = ""
	m._stableHistoryContent = ""
	m._lastSetContent = ""
	m._viewportDirty = false
	m._tokenCoalescePending = false
	m.ActiveFiles = nil
	m.DiffBlocks = nil
	m.ActiveDiffID = ""
}

// fetchModelsCmd returns a command that fetches models from the provider asynchronously.
func fetchModelsCmd(provider, apiKey, baseURL string) tea.Cmd {
	// Run the HTTP call in a goroutine so the TUI never blocks.
	// The returned Cmd races the goroutine and returns nil until done.
	fmt.Fprintf(os.Stderr, "[DEBUG] fetchModelsCmd called: provider=%s, baseURL=%s\n", provider, baseURL)
	done := make(chan tea.Msg, 1)
	go func() {
		defer func() {
			if r := recover(); r != nil {
				fmt.Fprintf(os.Stderr, "[DEBUG] fetchModelsCmd panic: %v\n", r)
				done <- SetupModelsFetchedMsg{Error: fmt.Sprintf("panic: %v", r)}
			}
		}()
		fmt.Fprintf(os.Stderr, "[DEBUG] fetchModelsCmd goroutine starting\n")
		models, err := fetchModelsFromProvider(provider, apiKey, baseURL)
		if err != nil {
			fmt.Fprintf(os.Stderr, "[DEBUG] fetchModelsCmd error: %v\n", err)
			done <- SetupModelsFetchedMsg{Error: err.Error()}
		} else {
			fmt.Fprintf(os.Stderr, "[DEBUG] fetchModelsCmd success: %d models\n", len(models))
			done <- SetupModelsFetchedMsg{Models: models}
		}
	}()
	return func() tea.Msg {
		return <-done
	}
}

// testConnectionCmd returns a command that tests the connection asynchronously.
func testConnectionCmd(provider, apiKey, baseURL, model string) tea.Cmd {
	done := make(chan tea.Msg, 1)
	go func() {
		defer func() {
			if r := recover(); r != nil {
				fmt.Fprintf(os.Stderr, "[DEBUG] testConnectionCmd panic: %v\n", r)
				done <- SetupTestResultMsg{Passed: false, Result: fmt.Sprintf("panic: %v", r)}
			}
		}()
		passed, result := testConnection(provider, apiKey, baseURL, model)
		done <- SetupTestResultMsg{Passed: passed, Result: result}
	}()
	return func() tea.Msg {
		return <-done
	}
}

// saveConfigCmd returns a command that saves the configuration asynchronously.
func saveConfigCmd(provider, baseURL, apiKey, apiKeyEnv, model, headers string) tea.Cmd {
	done := make(chan tea.Msg, 1)
	go func() {
		defer func() {
			if r := recover(); r != nil {
				fmt.Fprintf(os.Stderr, "[DEBUG] saveConfigCmd panic: %v\n", r)
				done <- SetupSaveResultMsg{Success: false, Error: fmt.Sprintf("panic: %v", r)}
			}
		}()
		err := saveConfig(provider, baseURL, apiKey, apiKeyEnv, model, headers)
		if err != nil {
			done <- SetupSaveResultMsg{Success: false, Error: err.Error()}
		} else {
			done <- SetupSaveResultMsg{Success: true}
		}
	}()

	return func() tea.Msg {
		return <-done
	}
}

// fetchModelsFromProvider fetches available models from the provider API.
func fetchModelsFromProvider(provider, apiKey, baseURL string) ([]string, error) {
	if apiKey == "" {
		return nil, fmt.Errorf("no API key provided")
	}
	keyPreview := apiKey
	if len(keyPreview) > 10 {
		keyPreview = keyPreview[:10]
	}
	fmt.Fprintf(os.Stderr, "[DEBUG] fetchModelsFromProvider: provider=%s, baseURL=%s, apiKey=%s\n", provider, baseURL, keyPreview+"...")

	// For Anthropic, return a hardcoded list since they don't have a public models API
	if provider == "anthropic" {
		return []string{
			"claude-3-5-sonnet-20241022",
			"claude-3-5-haiku-20241022",
			"claude-3-opus-20240229",
			"claude-3-sonnet-20240229",
			"claude-3-haiku-20240307",
		}, nil
	}

	// Ensure baseURL doesn't end with / and the path starts with /
	baseURL = strings.TrimSuffix(baseURL, "/")
	url := baseURL + "/models"
	
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(apiKey))
	req.Header.Set("Content-Type", "application/json")

	// Use 10 second timeout to prevent UI lockup
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("HTTP request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		body, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("API returned status %d: %s", resp.StatusCode, string(body))
	}

	// Try to parse the most common model-list shapes. Some APIs return
	// {"data":[{"id":"..."}]}, some return a bare [{"id":"..."}], and some use
	// {"models":[{"id":"..."}]} or {"models":["..."]}.
	rawBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	models := extractModelIDs(rawBody)
	if len(models) == 0 {
		return nil, fmt.Errorf("no models returned from API")
	}

	return models, nil
}

// extractModelIDs extracts model identifiers from a /models response body.
// It handles OpenAI-style {"data":[{"id":"..."}]}, a bare array, and the
// {"models": ...} variants used by some providers.
func extractModelIDs(body []byte) []string {
	// First try the standard OpenAI {"data":[{"id":"..."}]} shape.
	var openai struct {
		Data []struct {
			ID   string `json:"id"`
			Name string `json:"name"`
		} `json:"data"`
		Models []struct {
			ID   string `json:"id"`
			Name string `json:"name"`
		} `json:"models"`
	}
	if err := json.Unmarshal(body, &openai); err == nil {
		var ids []string
		for _, m := range openai.Data {
			if m.ID != "" {
				ids = append(ids, m.ID)
			} else if m.Name != "" {
				ids = append(ids, m.Name)
			}
		}
		for _, m := range openai.Models {
			if m.ID != "" {
				ids = append(ids, m.ID)
			} else if m.Name != "" {
				ids = append(ids, m.Name)
			}
		}
		if len(ids) > 0 {
			return ids
		}
	}

	// Try a bare array of objects [{"id":"..."}].
	var objArray []struct {
		ID   string `json:"id"`
		Name string `json:"name"`
	}
	if err := json.Unmarshal(body, &objArray); err == nil {
		var ids []string
		for _, m := range objArray {
			if m.ID != "" {
				ids = append(ids, m.ID)
			} else if m.Name != "" {
				ids = append(ids, m.Name)
			}
		}
		if len(ids) > 0 {
			return ids
		}
	}

	// Try a bare array of strings ["model-1", "model-2"].
	var strArray []string
	if err := json.Unmarshal(body, &strArray); err == nil && len(strArray) > 0 {
		return strArray
	}

	return nil
}

// testConnection tests the connection to the provider API.
func testConnection(provider, apiKey, baseURL, model string) (bool, string) {
	if apiKey == "" {
		return false, "No API key configured"
	}

	var url string
	var payload []byte
	var err error

	if provider == "anthropic" {
		url = baseURL + "/v1/messages"
		payload, err = json.Marshal(map[string]interface{}{
			"model":      model,
			"max_tokens": 10,
			"messages":   []map[string]interface{}{{ "role": "user", "content": "ping" }},
		})
	} else {
		// OpenAI-compatible API
		url = baseURL + "/chat/completions"
		payload, err = json.Marshal(map[string]interface{}{
			"model":       model,
			"max_tokens":  10,
			"messages":    []map[string]string{{"role": "user", "content": "ping"}},
		})
	}

	if err != nil {
		return false, "Failed to create request: " + err.Error()
	}

	req, err := http.NewRequest("POST", url, strings.NewReader(string(payload)))
	if err != nil {
		return false, err.Error()
	}
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(apiKey))
	req.Header.Set("Content-Type", "application/json")
	if provider == "anthropic" {
		req.Header.Set("anthropic-version", "2023-06-01")
	}

	// Use 5 second timeout to prevent UI lockup
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return false, "Connection failed: " + err.Error()
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		return true, fmt.Sprintf("%s / %s", provider, model)
	}

	body, _ := io.ReadAll(resp.Body)
	return false, fmt.Sprintf("API returned status %d: %s", resp.StatusCode, string(body))
}

// saveConfig saves the configuration to ~/.px/config.json
func saveConfig(provider, baseURL, apiKey, apiKeyEnv, model, headers string) error {
	configPath := os.Getenv("HOME") + "/.px/config.json"

	config := map[string]interface{}{
		"provider": provider,
		"model":    model,
	}
	if baseURL != "" {
		config["base_url"] = baseURL
	}
	if apiKeyEnv != "" {
		config["api_key_env"] = apiKeyEnv
	} else if apiKey != "" {
		config["api_key"] = apiKey
	}
	if headers != "" {
		config["headers"] = headers
	}

	data, err := json.MarshalIndent(config, "", "  ")
	if err != nil {
		return err
	}

	return os.WriteFile(configPath, data, 0644)
}
