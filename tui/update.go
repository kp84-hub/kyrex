package tui

import (
	"time"

	"github.com/charmbracelet/bubbles/textarea"
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
	Mode          string
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

func (m Model) Init() tea.Cmd {
	return tea.Batch(textarea.Blink, Tick(), FastTick())
}

// Update is the main Bubble Tea update dispatcher.
// It routes messages to focused handlers in separate files:
//   - update_keys.go    → handleKeyMsg
//   - update_mouse.go   → handleMouseMsg
//   - update_engine.go  → handleEngineMsg
func (m Model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	var (
		tiCmd tea.Cmd
		vpCmd tea.Cmd
		cmds  []tea.Cmd
	)

	switch msg := msg.(type) {
	case tea.KeyMsg:
		// Paste burst detection: track keystroke timing
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
		// Only auto-hide sidebar on initial boot if terminal is narrow (< 120 cols).
		if m.Width == 0 && msg.Width < 120 {
			m.ShowSidebar = false
		}
		m.Width = msg.Width
		m.Height = msg.Height
		layout := m.recalculateLayout()
		m.applyLayout(layout)

	case FastTickMsg:
		// Flush viewport if dirty from token/reasoning accumulation (50ms throttle)
		throttle := 150 * time.Millisecond
		if m.Reasoning != "" || m.CurrToken != "" {
			throttle = 50 * time.Millisecond
		}
		if m._viewportDirty && time.Since(m._lastViewportFlush) > throttle {
			newContent := m.FullViewportContent(m.Viewport.Width)
			// Only call SetContent if content actually changed (avoids full viewport recalc)
			if newContent != m._lastSetContent {
				m.Viewport.SetContent(newContent)
				m._lastSetContent = newContent
				if !m.ScrollLock {
					m.Viewport.GotoBottom()
				}
			}
			m._lastViewportFlush = time.Now()
			m._viewportDirty = false
		}
		// Continuous auto-scroll during selection
		if m.Selecting && m.AutoScrollDir != 0 {
			if m.AutoScrollDir > 0 {
				m.Viewport.LineDown(3)
			} else {
				m.Viewport.LineUp(3)
			}
			m._viewportDirty = true
		}
		cmds = append(cmds, FastTick())

	case TickMsg:
		if m.IsThinking {
			m.Timer++
		}
		throttle := 150 * time.Millisecond
		if m.Reasoning != "" || m.CurrToken != "" {
			throttle = 50 * time.Millisecond
		}
		if m._viewportDirty && time.Since(m._lastViewportFlush) > throttle {
			newContent := m.FullViewportContent(m.Viewport.Width)
			// Only call SetContent if content actually changed (avoids full viewport recalc)
			if newContent != m._lastSetContent {
				m.Viewport.SetContent(newContent)
				m._lastSetContent = newContent
				if !m.ScrollLock {
					m.Viewport.GotoBottom()
				}
			}
			m._lastViewportFlush = time.Now()
			m._viewportDirty = false
		}
			if m.Toast != "" && time.Now().After(m.ToastEnd) {
		m.Toast = ""
		m._viewportDirty = true
	}
		cmds = append(cmds, Tick())

	case MsgFromEngine:
		m, _, _ = m.handleEngineMsg(msg)
	}

	// Only pass keyboard messages to textarea — mouse events cause phantom line stacking
	switch msg.(type) {
	case tea.KeyMsg:
		m.Textarea, tiCmd = m.Textarea.Update(msg)
		// Textarea height is now fixed at 1 line to prevent layout shifts and flickering.
		// Multi-line input is still supported via Shift+Enter, but the textarea doesn't grow.
	default:
		tiCmd = nil
	}
	
	// Only pass messages to viewport that it actually needs to handle.
	// This prevents unnecessary recalculations on every keystroke.
	// Viewport only needs: mouse events (wheel scroll), navigation keys, window resizes.
	shouldUpdateViewport := false
	switch msg.(type) {
	case tea.MouseMsg:
		shouldUpdateViewport = true
	case tea.WindowSizeMsg:
		shouldUpdateViewport = true
	case tea.KeyMsg:
		// Only pass navigation keys to viewport, not regular character input
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

	return m, tea.Batch(cmds...)
}

// resetTurnState clears all per-turn telemetry before starting a new request.
func (m *Model) resetTurnState() {
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
	m._lastRenderTime = time.Now()
	m.ActiveFiles = nil
	m.DiffBlocks = nil
	m.ActiveDiffID = ""
}
