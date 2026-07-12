package tui

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"github.com/kp84-hub/kx/internal/race"
)

// ── Message Types ──────────────────────────────────────────────────────────

// ConsultSetupMsg is returned by the consult setup command after cloning and spawning.
type ConsultSetupMsg struct {
	Race      *race.Race
	Err       error
	CloneSecs []float64
}

// ConsultCompleteMsg carries results from all helpers after they settle.
type ConsultCompleteMsg struct {
	Diffs     map[int]string
	DiffLines map[int]int
	Gates     map[int]bool
	GateOuts  map[int]string
}

// ── Context Extraction ─────────────────────────────────────────────────────

// extractContext builds a text-only summary of recent conversation history,
// capped at maxBytes (default 6KB). It iterates from most recent to oldest,
// skipping noisy entries (_Logs:_ and _DiffContent:_), and stops when the
// cap would be exceeded. The returned string is in chronological order.
func extractContext(history []string, maxBytes int) string {
	if maxBytes <= 0 {
		maxBytes = 6 * 1024
	}

	var entries []string
	size := 0

	for i := len(history) - 1; i >= 0; i-- {
		entry := history[i]
		// Skip tool-result noise
		if strings.HasPrefix(entry, "_Logs:_") ||
			strings.HasPrefix(entry, "_DiffContent:_") ||
			strings.HasPrefix(entry, "_Thinking:_") {
			continue
		}
		if size+len(entry)+1 > maxBytes {
			break
		}
		entries = append(entries, entry)
		size += len(entry) + 1
	}

	// Reverse to chronological order
	var sb strings.Builder
	for i := len(entries) - 1; i >= 0; i-- {
		if sb.Len() > 0 {
			sb.WriteString("\n")
		}
		sb.WriteString(entries[i])
	}
	return sb.String()
}

// ── Parsing ────────────────────────────────────────────────────────────────

// parseConsultCommand parses "/consult [<focus text>] --models a[,b]"
// Returns focus text, models list, and parse error.
// A bare "/consult" returns (focus="", models=nil, err=nil) → wizard.
func parseConsultCommand(input string) (focus string, models []string, err error) {
	rest := strings.TrimSpace(strings.TrimPrefix(input, "/consult"))
	if rest == "" {
		return "", nil, nil // bare /consult → wizard
	}

	idx := strings.LastIndex(rest, "--models")
	if idx < 0 {
		return "", nil, fmt.Errorf("missing --models flag\nUsage: /consult [<focus>] --models <model1>[,<model2>] (max 2)")
	}

	focus = strings.TrimSpace(rest[:idx])
	modelsStr := strings.TrimSpace(rest[idx+len("--models"):])

	if modelsStr == "" {
		return "", nil, fmt.Errorf("model list is empty")
	}

	raw := strings.Split(modelsStr, ",")
	for _, m := range raw {
		m = strings.TrimSpace(m)
		if m != "" {
			models = append(models, m)
		}
	}

	if len(models) == 0 {
		return "", nil, fmt.Errorf("no valid models specified")
	}
	if len(models) > 2 {
		return "", nil, fmt.Errorf("max 2 models per consult (got %d)", len(models))
	}

	return focus, models, nil
}

// ── Consult Setup Command ──────────────────────────────────────────────────

// startConsultCmd returns a tea.Cmd that clones lanes, spawns engine
// subprocesses, and sends ConsultSetupMsg back when done (or on error).
// history is the main session's m.History used for context extraction.
func startConsultCmd(focus string, models []string, history []string, srcDir string) tea.Cmd {
	return func() tea.Msg {
		// Construct engine command matching main.go: bundled binary or python bridge.
		exe, err := os.Executable()
		if err != nil {
			return ConsultSetupMsg{Err: fmt.Errorf("cannot find executable: %w", err)}
		}
		workspaceRoot := filepath.Dir(exe)

		var engineCmd []string
		bundledEngine := filepath.Join(workspaceRoot, "kyrex-engine")
		if _, statErr := os.Stat(bundledEngine); statErr == nil {
			engineCmd = []string{bundledEngine}
		} else {
			homeDir, _ := os.UserHomeDir()
			bridgeScript := filepath.Join(homeDir, "kyrex", "kyrex_engine", "core_bridge.py")
			engineCmd = []string{"python3", bridgeScript}
		}

		homeDir, _ := os.UserHomeDir()
		consultsDir := filepath.Join(homeDir, ".kx", "consults")
		if err := os.MkdirAll(consultsDir, 0755); err != nil {
			return ConsultSetupMsg{Err: fmt.Errorf("mkdir consults dir: %w", err)}
		}

		// Build helper task
		contextText := extractContext(history, 6*1024)
		var task strings.Builder
		task.WriteString("You are assisting another engineer mid-debugging. Context of their session so far:\n")
		task.WriteString(contextText)
		task.WriteString("\n\n")
		task.WriteString("FOCUS:\n")
		if focus != "" {
			task.WriteString(focus)
		} else {
			task.WriteString("Continue diagnosing/fixing the problem described above.")
		}
		task.WriteString("\n\nWork independently in this workspace. Make your best attempt at a concrete fix. Keep changes minimal.")

		// Sweep abandoned consults before creating a new one.
		abandoned, _ := race.FindAbandoned(consultsDir)
		for _, m := range abandoned {
			_ = os.RemoveAll(m.RaceDir)
		}

		consultDir := filepath.Join(consultsDir, fmt.Sprintf("consult-%d", time.Now().Unix()))

		r, err := race.New(task.String(), srcDir, consultDir, models)
		if err != nil {
			return ConsultSetupMsg{Err: err}
		}

		// Spawn and start each lane.
		for _, l := range r.Lanes {
			if l == nil {
				continue
			}
			if err := l.Spawn(engineCmd, consultDir); err != nil {
				_ = r.Cleanup()
				return ConsultSetupMsg{Err: fmt.Errorf("lane %d spawn: %w", l.ID, err)}
			}
			if err := l.StartReader(func(msg any) {
				if Program != nil {
					Program.Send(msg)
				}
			}); err != nil {
				_ = r.Cleanup()
				return ConsultSetupMsg{Err: fmt.Errorf("lane %d reader: %w", l.ID, err)}
			}
		}

		if err := r.WriteManifest(); err != nil {
			return ConsultSetupMsg{Err: fmt.Errorf("write manifest: %w", err)}
		}

		return ConsultSetupMsg{Race: r, CloneSecs: r.CloneSecs}
	}
}

// consultTickCmd returns a command that fires every 3 seconds for consult upkeep.
func consultTickCmd() tea.Cmd {
	return tea.Tick(3*time.Second, func(t time.Time) tea.Msg {
		return ConsultTickMsg(t)
	})
}

// ConsultTickMsg fires periodically while consult is active to check round caps.
type ConsultTickMsg time.Time

// ── Consult Complete Processing ───────────────────────────────────────────

// consultDiffsAndGatesCmd runs DiffLane and GateLane per done lane concurrently.
func consultDiffsAndGatesCmd(r *race.Race) tea.Cmd {
	return func() tea.Msg {
		diffs := make(map[int]string)
		diffLines := make(map[int]int)
		gates := make(map[int]bool)
		gateOuts := make(map[int]string)
		var mu sync.Mutex
		var wg sync.WaitGroup

		for _, l := range r.Lanes {
			if l == nil {
				continue
			}
			wg.Add(1)
			go func(lane *race.Lane) {
				defer wg.Done()

				// Diff
				d, err := r.DiffLane(lane)
				mu.Lock()
				if err != nil {
					diffs[lane.ID] = ""
					diffLines[lane.ID] = 0
				} else {
					diffs[lane.ID] = d
					lines := 0
					if d != "" {
						lines = len(strings.Split(d, "\n"))
					}
					diffLines[lane.ID] = lines
				}
				mu.Unlock()

				// Gate (runs outside lock for max concurrency)
				cmd := race.DefaultGateCommand(lane.Dir)
				passed, out, _ := r.GateLane(lane, cmd, 120*time.Second)
				mu.Lock()
				gates[lane.ID] = passed
				gateOuts[lane.ID] = out
				mu.Unlock()
			}(l)
		}
		wg.Wait()

		return ConsultCompleteMsg{
			Diffs:     diffs,
			DiffLines: diffLines,
			Gates:     gates,
			GateOuts:  gateOuts,
		}
	}
}

// helperLetter returns a letter label (A, B, C, D) for a lane ID.
func helperLetter(id int) string {
	letters := []string{"A", "B", "C", "D"}
	if id >= 0 && id < len(letters) {
		return letters[id]
	}
	return fmt.Sprintf("?%d", id)
}

// buildConsultInjection constructs the injection message from consult results.
func buildConsultInjection(r *race.Race, diffs map[int]string, gates map[int]bool, gateOuts map[int]string) string {
	var sb strings.Builder
	sb.WriteString("═══ Consult results ═══\n\n")

	for i, l := range r.Lanes {
		if l == nil {
			continue
		}
		letter := helperLetter(l.ID)

		gateStatus := "FAILED"
		if gates[i] {
			gateStatus = "PASSED"
		}

		// Include model name — main model benefits from knowing sources
		sb.WriteString(fmt.Sprintf("Helper %s (%s): gate %s. Rounds: %d.\n", letter, l.Model, gateStatus, l.Rounds))

		diffText := diffs[i]
		if diffText != "" {
			const diffCap = 12 * 1024 // 12KB tail-truncated
			if len(diffText) > diffCap {
				diffText = diffText[len(diffText)-diffCap:]
			}
			sb.WriteString("Diff:\n")
			sb.WriteString(diffText)
			sb.WriteString("\n")
		} else {
			sb.WriteString("Diff: no changes.\n")
		}

		if !gates[i] {
			gateOut := gateOuts[i]
			if len(gateOut) > 1024 {
				gateOut = gateOut[len(gateOut)-1024:]
			}
			sb.WriteString("Gate output (last 1KB):\n")
			sb.WriteString(gateOut)
			sb.WriteString("\n")
		}

		sb.WriteString("\n")
	}

	sb.WriteString("These are independent attempts by other models, verified by build/test where noted. Evaluate them against everything we know from this session: adopt, combine, or reject their approaches with reasons, and continue solving the problem.\n")

	return sb.String()
}

// ── Lane Message Handling ─────────────────────────────────────────────────

// handleConsultLaneMsg processes a LaneMsg during an active consult.
func (m Model) handleConsultLaneMsg(msg race.LaneMsg) Model {
	if !m._consultActive || m._consult == nil {
		return m
	}
	if msg.LaneID < 0 || msg.LaneID >= len(m._consult.Lanes) {
		return m
	}
	l := m._consult.Lanes[msg.LaneID]
	if l == nil {
		return m
	}
	ev := msg.Event

	// Deferred task send: on session_state, deliver the task if not yet sent.
	if ev.Type == "session_state" && !m._consultTaskSent[msg.LaneID] {
		if err := l.SendLine(map[string]any{
			"type":    "chat",
			"content": m._consult.Task,
		}); err != nil {
			l.Status = race.LaneFailed
			l.Err = fmt.Sprintf("send task: %v", err)
			l.FinishedAt = time.Now()
		}
		if m._consultTaskSent == nil {
			m._consultTaskSent = make(map[int]bool)
		}
		m._consultTaskSent[msg.LaneID] = true
	}

	switch {
	case ev.IsTool():
		l.Rounds++
		l.LastTool = ev.Name
	case ev.IsDone():
		if m._consultTaskSent[msg.LaneID] {
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

	return m
}

// handleConsultLaneExit processes a LaneExitMsg during an active consult.
func (m Model) handleConsultLaneExit(msg race.LaneExitMsg) Model {
	if !m._consultActive || m._consult == nil {
		return m
	}
	if msg.LaneID < 0 || msg.LaneID >= len(m._consult.Lanes) {
		return m
	}
	l := m._consult.Lanes[msg.LaneID]
	if l == nil {
		return m
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
	return m
}

// deliverConsultInjection sends the consult results to the main engine and
// cleans up the consult directory.
func (m Model) deliverConsultInjection(msg ConsultCompleteMsg) Model {
	if m._consult == nil {
		return m
	}

	// Build injection message
	injection := buildConsultInjection(m._consult, msg.Diffs, msg.Gates, msg.GateOuts)

	// Handle failure cases: if all helpers failed, just note it
	allFailed := true
	for _, l := range m._consult.Lanes {
		if l != nil && l.Status == race.LaneDone {
			allFailed = false
			break
		}
	}

	if allFailed {
		// Inject a one-liner instead
		var errs []string
		for _, l := range m._consult.Lanes {
			if l != nil && l.Err != "" {
				errs = append(errs, fmt.Sprintf("Helper %s: %s", helperLetter(l.ID), l.Err))
			}
		}
		injection = "═══ Consult results ═══\nAll helpers failed: " + strings.Join(errs, "; ") + "\nContinuing with main session.\n"
	}

	// Build summary line for history
	var summaryParts []string
	for _, l := range m._consult.Lanes {
		if l != nil {
			gateStr := "✗"
			if msg.Gates[l.ID] {
				gateStr = "✓"
			}
			summaryParts = append(summaryParts, fmt.Sprintf("%s: gate %s", helperLetter(l.ID), gateStr))
		}
	}
	historyLine := "Consult complete: helper diffs injected (" + strings.Join(summaryParts, ", ") + ")."

	// Send injection to main engine as a normal chat message
	if m.SendFunc != nil {
		m.SendFunc(map[string]interface{}{
			"type":    "chat",
			"content": injection,
		})
	}

	// Add history line
	m.History = append(m.History, "> "+historyLine)
	m.History = append(m.History, "_Overview:_\n"+historyLine)

	// Cleanup consult dir (helpers are advisory — not merged)
	_ = m._consult.Cleanup()

	// Reset consult state
	m._consult = nil
	m._consultActive = false
	m._consultTaskSent = nil

	// Refresh viewport
	m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
	m.Viewport.GotoBottom()

	return m
}

// cleanupConsult cancels an active consult without injecting results.
func (m Model) cleanupConsult() Model {
	if m._consult != nil {
		_ = m._consult.Cleanup()
	}
	m._consult = nil
	m._consultActive = false
	m._consultTaskSent = nil
	m._consultWizardStep = 0
	m._consultWizardTask = ""
	m._consultModelPickerActive = false
	m._consultModelPickerLoading = false
	m._consultModelPickerAll = nil
	m._consultModelPickerItems = nil
	m._consultModelPickerFilter = ""
	m._consultModelPickerIndex = 0
	m._consultModelPickerSelected = nil
	m._consultConfirmPending = false
	m._consultConfirmModels = nil
	m._consultConfirmFocus = ""
	m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
	m.Viewport.GotoBottom()
	return m
}

// ── Rendering ──────────────────────────────────────────────────────────────

// RenderConsultView renders the consult lane panes with a CONSULT header.
func (m Model) RenderConsultView() string {
	if m._consult == nil {
		return "Initializing consult..."
	}

	paneWidth := m.Width - 4
	if paneWidth < 20 {
		paneWidth = 20
	}

	headerStyle := lipgloss.NewStyle().Bold(true).Foreground(purple)
	header := headerStyle.Render("═══ CONSULT ═══  helpers working on current problem")

	var panes []string

	for _, l := range m._consult.Lanes {
		if l == nil {
			continue
		}

		// Compute elapsed time.
		elapsed := ""
		if !l.StartedAt.IsZero() {
			end := time.Now()
			if !l.FinishedAt.IsZero() {
				end = l.FinishedAt
			}
			d := end.Sub(l.StartedAt).Round(time.Second)
			if d < 0 {
				d = 0
			}
			elapsed = d.String()
		}

		icon := laneStatusIcon(l.Status)
		color := laneStatusColor(l.Status)
		statusStr := l.Status.String()

		headerLine := lipgloss.NewStyle().Bold(true).Render(
			fmt.Sprintf("Helper %s  %s  %s", helperLetter(l.ID), icon, l.Model))

		infoLine := fmt.Sprintf("Status: %s  |  Rounds: %d  |  Elapsed: %s",
			statusStr, l.Rounds, elapsed)

		toolLine := ""
		if l.LastTool != "" {
			toolLine = "Last tool: " + l.LastTool
		}

		errLine := ""
		if l.Err != "" {
			errLine = lipgloss.NewStyle().Foreground(red).Render("Error: " + l.Err)
		}

		// Tail lines (up to 3).
		tail := l.Tail()
		var tailLines []string
		for _, t := range tail {
			maxW := paneWidth - 8
			if maxW < 10 {
				maxW = 10
			}
			if len(t) > maxW {
				t = t[:maxW-1] + "…"
			}
			tailLines = append(tailLines, lipgloss.NewStyle().Foreground(subtle).Render(t))
		}

		var content strings.Builder
		content.WriteString(headerLine + "\n")
		content.WriteString(lipgloss.NewStyle().Foreground(color).Render(infoLine) + "\n")
		if toolLine != "" {
			content.WriteString(lipgloss.NewStyle().Foreground(subtle).Render(toolLine) + "\n")
		}
		if errLine != "" {
			content.WriteString(errLine + "\n")
		}
		if len(tailLines) > 0 {
			for _, tl := range tailLines {
				content.WriteString(tl + "\n")
			}
		}

		pane := lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder()).
			BorderForeground(purple).
			Padding(0, 1).
			Width(paneWidth).
			Render(strings.TrimSpace(content.String()))

		panes = append(panes, pane)
	}

	if len(panes) == 0 {
		return "No helpers."
	}

	consultContent := header + "\n" + lipgloss.JoinVertical(lipgloss.Left, panes...)
	return consultContent
}

// RenderConsultModelPicker renders the multi-select model picker for consult (max 2).
func (m Model) RenderConsultModelPicker(width int) string {
	if width < 10 {
		width = 10
	}

	titleStyle := lipgloss.NewStyle().Foreground(accent).Bold(true)
	itemStyle := lipgloss.NewStyle().Foreground(fg)
	highlightStyle := lipgloss.NewStyle().Foreground(accent).Bold(true)
	dimStyle := lipgloss.NewStyle().Foreground(subtle)

	items := m._consultModelPickerItems
	if items == nil {
		items = []string{}
	}

	var sb strings.Builder

	// Title line with count
	selCount := len(m._consultModelPickerSelected)
	sb.WriteString(titleStyle.Render(fmt.Sprintf("Consult models (%d/2 selected):", selCount)))
	if selCount > 0 {
		sb.WriteString(" ")
		for i, sel := range m._consultModelPickerSelected {
			if i > 0 {
				sb.WriteString(", ")
			}
			sb.WriteString(dimStyle.Render(sel))
		}
	}
	sb.WriteString("\n")

	// Filter line
	sb.WriteString(dimStyle.Render("Filter: ") + m._consultModelPickerFilter + dimStyle.Render("█") + "\n")

	// Items
	for i, model := range items {
		prefix := "  "
		if i == m._consultModelPickerIndex {
			prefix = "▶ "
		}
		isSelected := false
		for _, sel := range m._consultModelPickerSelected {
			if sel == model {
				isSelected = true
				break
			}
		}
		check := ""
		if isSelected {
			check = "✓ "
		}
		if i == m._consultModelPickerIndex {
			sb.WriteString(highlightStyle.Render(prefix+check+model) + "\n")
		} else {
			sb.WriteString(itemStyle.Render(prefix+check+model) + "\n")
		}
	}
	if len(items) == 0 {
		sb.WriteString(dimStyle.Render("  (no matching models)") + "\n")
	}

	// Footer hint
	sb.WriteString("\n" + dimStyle.Render("space=select enter=start esc=cancel"))

	boxStyle := lipgloss.NewStyle().
		Border(lipgloss.RoundedBorder()).
		BorderForeground(accent).
		Padding(0, 1).
		Width(width - 4)

	return boxStyle.Render(sb.String())
}
