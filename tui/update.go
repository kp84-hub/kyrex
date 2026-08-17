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
	"github.com/kp84-hub/kx/internal/race"
)

// Program is the *tea.Program handle, used by race lane reader goroutines to
// inject LaneMsg / LaneExitMsg into the TUI event loop.
// Set in main.go immediately after tea.NewProgram.
var Program *tea.Program

// MsgFromEngine is the IPC message type received from the Python engine process.
type MCPConnectionResult struct {
	Success   bool   `json:"success"`
	Server    string `json:"server"`
	ToolCount int    `json:"tool_count"`
	Error     string `json:"error"`
}

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
	if !m.ScrollLock && wasAtBottom {
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
	)

	msgType := classifyMsg(msg)
	prevDirty := m._viewportDirty

	switch msg := msg.(type) {
	case tea.KeyMsg:
		prevKeyTime := m._lastKeyTime
		m._lastKeyTime = time.Now()

		m, cmd, handled := m.handleKeyMsg(msg, prevKeyTime)
		if handled {
			return m, cmd
		}

	case tea.MouseMsg:
		m, cmd, handled := m.handleMouseMsg(msg)
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
		if m._timerActive {
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
	case AutoApproveFireMsg:
		if m.ConfirmID != "" && m.ConfirmID == msg.ConfirmID {
			m = m.approveConfirm()
		}

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

		// Consult model picker fetch result
		if m._consultModelPickerLoading {
			if msg.Error != "" {
				m._consultModelPickerLoading = false
				m.History = append(m.History, "Model fetch failed: "+msg.Error+" — type models comma-separated instead:")
				m.History = append(m.History, "Models? (comma-separated, max 2 — e.g. kimi-k2.7-code,glm-5.2)")
				m.Textarea.Reset()
				m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
				m.Viewport.GotoBottom()
			} else {
				m._consultModelPickerAll = msg.Models
				m._consultModelPickerItems = msg.Models
				m._consultModelPickerFilter = ""
				m._consultModelPickerIndex = 0
				m._consultModelPickerLoading = false
				m._consultModelPickerActive = true
				m._viewportDirty = true
				m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
			}
			return m, nil
		}

		// Race model picker fetch result
		if m._raceModelPickerLoading {
			if msg.Error != "" {
				m._raceModelPickerLoading = false
				m.History = append(m.History, "Model fetch failed: "+msg.Error+" — type models comma-separated instead:")
				m.History = append(m.History, "Models? (comma-separated, max 4 — e.g. deepseek/deepseek-v4-flash,tencent/hy3-preview)")
				m.Textarea.Reset()
				m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
				m.Viewport.GotoBottom()
			} else {
				m._raceModelPickerAll = msg.Models
				m._raceModelPickerItems = msg.Models
				m._raceModelPickerFilter = ""
				m._raceModelPickerIndex = 0
				m._raceModelPickerLoading = false
				m._raceModelPickerActive = true
				m._viewportDirty = true
				m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
			}
			return m, nil
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

	// ── Consult mode messages ──────────────────────────────────────────

	case ConsultSetupMsg:
		if msg.Err != nil {
			m.History = append(m.History, "Consult setup failed: "+msg.Err.Error())
			m._consultActive = false
			m._consult = nil
			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
			return m, nil
		}
		m._consult = msg.Race
		m._consultTaskSent = make(map[int]bool, len(msg.Race.Lanes))
		var cloneInfo string
		for i, secs := range msg.CloneSecs {
			if i > 0 {
				cloneInfo += ", "
			}
			modelName := ""
			if i < len(msg.Race.Lanes) && msg.Race.Lanes[i] != nil {
				modelName = msg.Race.Lanes[i].Model
			}
			cloneInfo += fmt.Sprintf("helper %d (%s): %.1fs", i, modelName, secs)
		}
		m.History = append(m.History, "Helper clones ready: "+cloneInfo)
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		m.Viewport.GotoBottom()
		cmds = append(cmds, consultTickCmd())

	case ConsultCompleteMsg:
		if !m._consultActive && m._consult == nil {
			break
		}
		m = m.deliverConsultInjection(msg)

	case ConsultTickMsg:
		if m._consultActive && m._consult != nil {
			for _, l := range m._consult.Lanes {
				if l == nil {
					continue
				}
				if l.Status == race.LaneRunning && l.Rounds >= m._consult.RoundCap {
					l.Kill()
				}
			}
			if m._consult.AllSettled() {
				m._consultActive = false // mark inactive so viewport restores
				cmds = append(cmds, consultDiffsAndGatesCmd(m._consult))
			} else {
				cmds = append(cmds, consultTickCmd())
			}
		}

	// ── Race mode messages ───────────────────────────────────────────

	case RaceSetupMsg:
		if msg.Err != nil {
			m.History = append(m.History, "Race setup failed: "+msg.Err.Error())
			m.RaceMode = false
			m.Race = nil
			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
			return m, nil
		}
		m.Race = msg.Race
		m._raceTaskSent = make(map[int]bool, len(msg.Race.Lanes))
		var cloneInfo string
		for i, secs := range msg.CloneSecs {
			if i > 0 {
				cloneInfo += ", "
			}
			modelName := ""
			if i < len(msg.Race.Lanes) && msg.Race.Lanes[i] != nil {
				modelName = msg.Race.Lanes[i].Model
			}
			cloneInfo += fmt.Sprintf("lane %d (%s): %.1fs", i, modelName, secs)
		}
		m.History = append(m.History, "Clones ready: "+cloneInfo)
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		m.Viewport.GotoBottom()
		cmds = append(cmds, raceTickCmd())

	case race.LaneMsg:
		// Route to consult handler if consult is active
		if m._consultActive && m._consult != nil {
			m = m.handleConsultLaneMsg(msg)
			// If consult just settled, kick off diffs+gates
			if m._consult != nil && m._consult.AllSettled() {
				m._consultActive = false
				cmds = append(cmds, consultDiffsAndGatesCmd(m._consult))
			}
			break
		}
		if !m.RaceMode || m.Race == nil {
			break
		}
		if msg.LaneID < 0 || msg.LaneID >= len(m.Race.Lanes) {
			break
		}
		l := m.Race.Lanes[msg.LaneID]
		if l == nil {
			break
		}
		ev := msg.Event

		// Deferred task send: on session_state, deliver the task if not yet sent.
		if ev.Type == "session_state" && !m._raceTaskSent[msg.LaneID] {
			if err := l.SendLine(map[string]any{
				"type":    "chat",
				"content": m.Race.Task,
			}); err != nil {
				l.Status = race.LaneFailed
				l.Err = fmt.Sprintf("send task: %v", err)
				l.FinishedAt = time.Now()
			}
			m._raceTaskSent[msg.LaneID] = true
		}

		switch {
		case ev.IsTool():
			l.Rounds++
			l.LastTool = ev.Name
		case ev.IsDone():
			// Only treat chat_done as lane completion if the task was sent.
			// During engine init a chat_done may fire spuriously.
			if m._raceTaskSent[msg.LaneID] {
				l.Status = race.LaneDone
				l.FinishedAt = time.Now()
			}
		case ev.IsError():
			l.Status = race.LaneFailed
			l.Err = ev.ErrText()
			l.FinishedAt = time.Now()
		default:
			if ev.IsText() {
				l.AppendTail(ev.Content)
			}
		}
		if m.Race.AllSettled() {
			m = m.enterRaceComparing()
			cmds = append(cmds, computeRaceDiffsCmd(m.Race))
			cmds = append(cmds, runGatesCmd(m.Race))
		}

	case race.LaneExitMsg:
		// Route to consult handler if consult is active
		if m._consultActive && m._consult != nil {
			m = m.handleConsultLaneExit(msg)
			if m._consult != nil && m._consult.AllSettled() {
				m._consultActive = false
				cmds = append(cmds, consultDiffsAndGatesCmd(m._consult))
			}
			break
		}
		if !m.RaceMode || m.Race == nil {
			break
		}
		if msg.LaneID < 0 || msg.LaneID >= len(m.Race.Lanes) {
			break
		}
		l := m.Race.Lanes[msg.LaneID]
		if l == nil {
			break
		}
		if l.Status == race.LaneRunning || l.Status == race.LanePending {
			l.Status = race.LaneFailed
			if msg.Err != nil {
				l.Err = msg.Err.Error()
			} else {
				l.Err = "unexpected exit"
			}
			l.FinishedAt = time.Now()
		}
		if m.Race.AllSettled() {
			m = m.enterRaceComparing()
			cmds = append(cmds, computeRaceDiffsCmd(m.Race))
			cmds = append(cmds, runGatesCmd(m.Race))
		}

	case RaceTickMsg:
		if m.RaceMode && m.Race != nil {
			for _, l := range m.Race.Lanes {
				if l == nil {
					continue
				}
				if l.Status == race.LaneRunning && l.Rounds >= m.Race.RoundCap {
					l.Kill()
				}
			}
			if m.Race.AllSettled() {
				m = m.enterRaceComparing()
				cmds = append(cmds, computeRaceDiffsCmd(m.Race))
				cmds = append(cmds, runGatesCmd(m.Race))
			} else {
				cmds = append(cmds, raceTickCmd())
			}
		}

	// ── Race Comparing messages ──

	case RaceDiffMsg:
		if !m._raceComparing || m.Race == nil {
			break
		}
		if msg.Diffs != nil {
			m._raceDiffs = msg.Diffs
		}
		if msg.DiffLines != nil {
			m._raceDiffLines = msg.DiffLines
		}

	case RaceGatesMsg:
		if !m._raceComparing || m.Race == nil {
			break
		}
		m._raceGatesRunning = false
		if msg.Results != nil {
			m._raceGates = msg.Results
		}
		if msg.Outputs != nil {
			m._raceGateOutput = msg.Outputs
		}
		// If EXACTLY ONE lane passed its gate, move highlight to it
		var onlyPassing int
		passCount := 0
		for id, passed := range m._raceGates {
			if passed {
				passCount++
				onlyPassing = id
			}
		}
		if passCount == 1 {
			m._raceHighlight = onlyPassing
			m.History = append(m.History, fmt.Sprintf("Gate results: lane %d is the only passing lane.", onlyPassing))
			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
			m.Viewport.GotoBottom()
		}

	case RaceMergeResultMsg:
		if !m._raceComparing {
			break
		}
		m._raceMergePending = false
		if msg.Err != nil {
			m.History = append(m.History, fmt.Sprintf("Merge failed for Lane %d (%s): %v", msg.LaneID, msg.Model, msg.Err))
			if msg.Changes > 0 {
				m.History = append(m.History, fmt.Sprintf("  (partially merged %d changes before error)", msg.Changes))
			}
			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
			break
		}
		m.History = append(m.History, fmt.Sprintf("Merged Lane %d (%s): %d changes", msg.LaneID, msg.Model, msg.Changes))
		// Race overview injection: extract lane's work log and send to main model.
		if !m._raceNoOverview && m.Race != nil {
			laneIdx := msg.LaneID
			if laneIdx >= 0 && laneIdx < len(m.Race.Lanes) {
				l := m.Race.Lanes[laneIdx]
				if l != nil && l.Dir != "" {
					sessionPath := normalizeSessionPath(l.Dir)
					story, err := extractLaneStory(sessionPath, 4096)
					if err != nil {
						m.History = append(m.History, "Race overview unavailable: "+err.Error())
					} else if story != "" {
						diffPreview := ""
						if d, ok := m._raceDiffs[laneIdx]; ok && d != "" {
							if len(d) > 2048 {
								d = d[:2048]
							}
							diffPreview = d
						}
						injection := fmt.Sprintf("═══ Race overview ═══\nThe merged result came from %s (Lane %d, %d rounds). Its work log:\n%s\n\nDiff summary:\n%s\n\nBriefly summarize for the user what this model did and note anything worth reviewing.",
							l.Model, l.ID, l.Rounds, story, diffPreview)
						if m.SendFunc != nil {
							m.SendFunc(map[string]interface{}{
								"type":    "chat",
								"content": injection,
							})
						}
					}
				}
			}
		}
		// Merge succeeded — clean up and exit race mode
		m = m.discardRace()
	}

	// Only pass keyboard messages to textarea
	switch msg := msg.(type) {
	case tea.KeyMsg:
		// ── Paste detection & collapse ──
		// If a KeyRunes message arrives with many runes at once (>= 20 chars
		// or contains 2+ newlines), treat it as a paste: store the full text
		// in _realInputBuffer and replace the textarea's visible content with
		// a "[Pasted ~N lines]" placeholder. The full text is sent on submit.
		if msg.Type == tea.KeyRunes && len(msg.Runes) >= 20 {
			pasted := string(msg.Runes)
			// Append to the real buffer FIRST so the line count always
			// reflects the full accumulated content across all fragments
			// of a multi-part paste.
			m._realInputBuffer += pasted
			lineCount := strings.Count(m._realInputBuffer, "\n") + 1
			visible := m.Textarea.Value()
			if visible != "" {
				visible += "\n"
			}
			visible += fmt.Sprintf("[Pasted ~%d lines]", lineCount)
			m.Textarea.Reset()
			m.Textarea.SetValue(visible)
			m.Textarea.SetCursor(len([]rune(visible))) // end of visible text
			tiCmd = nil
		} else {
			m.Textarea, tiCmd = m.Textarea.Update(msg)
		}
	default:
		tiCmd = nil
	}

	// Only pass messages to viewport that it actually needs to handle
	shouldUpdateViewport := false
	// In race comparing mode, the viewport (chat history) is hidden behind
	// the race view — don't scroll it.
	if m.RaceMode && m._raceComparing {
		shouldUpdateViewport = false
	} else {
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

	// Splash ↔ chat transition: the splash renders the textarea in a centered,
	// width-capped box while chat mode spans the full main column. Re-apply
	// layout on the flip so the textarea's real width matches the active
	// render path without waiting for a window resize.
	if m.HasSentFirstMessage != m._lastHasSentFirstMessage {
		m._lastHasSentFirstMessage = m.HasSentFirstMessage
		m.applyLayout(m.recalculateLayout())
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
	m._interruptPending = false
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
	m._progressUpdateCount = 0
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
	done := make(chan tea.Msg, 1)
	go func() {
		defer func() {
			if r := recover(); r != nil {
				done <- SetupModelsFetchedMsg{Error: fmt.Sprintf("panic: %v", r)}
			}
		}()
		models, err := fetchModelsFromProvider(provider, apiKey, baseURL)
		if err != nil {
			done <- SetupModelsFetchedMsg{Error: err.Error()}
		} else {
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
			"messages":   []map[string]interface{}{{"role": "user", "content": "ping"}},
		})
	} else {
		// OpenAI-compatible API
		url = baseURL + "/chat/completions"
		payload, err = json.Marshal(map[string]interface{}{
			"model":      model,
			"max_tokens": 10,
			"messages":   []map[string]string{{"role": "user", "content": "ping"}},
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

// AutoApproveFireMsg is sent when an auto-approve timer completes.
type AutoApproveFireMsg struct {
	ConfirmID string
}

func autoApproveCmd(delay time.Duration, confirmID string) tea.Cmd {
	return tea.Tick(delay, func(t time.Time) tea.Msg {
		return AutoApproveFireMsg{ConfirmID: confirmID}
	})
}
