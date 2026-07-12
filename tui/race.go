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
	"github.com/kp84-hub/kx/internal/rift"
)

// ── Message Types ──────────────────────────────────────────────────────────

// RaceSetupMsg is returned by the race setup command after cloning and spawning.
type RaceSetupMsg struct {
	Race      *race.Race
	Err       error
	CloneSecs []float64
}

// RaceTickMsg fires periodically while a race is active to check round caps.
type RaceTickMsg time.Time

// ── Parsing ────────────────────────────────────────────────────────────────

// parseRaceCommand parses "/race <task text> --models a,b[,c]"
// Returns the task text, list of model strings, and any parse error.
func parseRaceCommand(input string) (task string, models []string, err error) {
	rest := strings.TrimSpace(strings.TrimPrefix(input, "/race"))

	idx := strings.LastIndex(rest, "--models")
	if idx < 0 {
		return "", nil, fmt.Errorf("missing --models flag\nUsage: /race <task> --models <model1>,<model2>[,...]")
	}

	task = strings.TrimSpace(rest[:idx])
	modelsStr := strings.TrimSpace(rest[idx+len("--models"):])

	if task == "" {
		return "", nil, fmt.Errorf("task text is empty")
	}
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
	if len(models) > 4 {
		return "", nil, fmt.Errorf("max 4 models per race (got %d)", len(models))
	}

	return task, models, nil
}

// ── Race Setup Command ─────────────────────────────────────────────────────

// startRaceCmd returns a tea.Cmd that clones lanes, spawns engine subprocesses,
// and sends RaceSetupMsg back when done (or on error).
func startRaceCmd(task string, models []string, srcDir string) tea.Cmd {
	return func() tea.Msg {
		// Construct engine command matching main.go: bundled binary or python bridge.
		exe, err := os.Executable()
		if err != nil {
			return RaceSetupMsg{Err: fmt.Errorf("cannot find executable: %w", err)}
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
		racesDir := filepath.Join(homeDir, ".kx", "races")
		if err := os.MkdirAll(racesDir, 0755); err != nil {
			return RaceSetupMsg{Err: fmt.Errorf("mkdir races dir: %w", err)}
		}

		// Sweep abandoned races before creating a new one.
		abandoned, _ := race.FindAbandoned(racesDir)
		for _, m := range abandoned {
			_ = os.RemoveAll(m.RaceDir)
		}

		raceDir := filepath.Join(racesDir, fmt.Sprintf("race-%d", time.Now().Unix()))

		r, err := race.New(task, srcDir, raceDir, models)
		if err != nil {
			return RaceSetupMsg{Err: err}
		}

		// Spawn and start each lane.
		for _, l := range r.Lanes {
			if l == nil {
				continue
			}
			if err := l.Spawn(engineCmd, raceDir); err != nil {
				_ = r.Cleanup()
				return RaceSetupMsg{Err: fmt.Errorf("lane %d spawn: %w", l.ID, err)}
			}
			if err := l.StartReader(func(msg any) {
				if Program != nil {
					Program.Send(msg)
				}
			}); err != nil {
				_ = r.Cleanup()
				return RaceSetupMsg{Err: fmt.Errorf("lane %d reader: %w", l.ID, err)}
			}
			// Task send is deferred to the LaneMsg handler so the engine
			// has time to finish initialization before receiving input.
		}

		if err := r.WriteManifest(); err != nil {
			return RaceSetupMsg{Err: fmt.Errorf("write manifest: %w", err)}
		}

		return RaceSetupMsg{Race: r, CloneSecs: r.CloneSecs}
	}
}

// raceTickCmd returns a command that fires every 3 seconds for race upkeep.
func raceTickCmd() tea.Cmd {
	return tea.Tick(3*time.Second, func(t time.Time) tea.Msg {
		return RaceTickMsg(t)
	})
}

// ── Rendering ──────────────────────────────────────────────────────────────

// laneStatusIcon returns a small visual indicator for a lane's status.
func laneStatusIcon(s race.LaneStatus) string {
	switch s {
	case race.LanePending:
		return "○"
	case race.LaneRunning:
		return "●"
	case race.LaneDone:
		return "✓"
	case race.LaneFailed:
		return "✗"
	case race.LaneKilled:
		return "⊘"
	default:
		return "?"
	}
}

// laneStatusColor returns the lipgloss colour for a lane status.
func laneStatusColor(s race.LaneStatus) lipgloss.Color {
	switch s {
	case race.LanePending:
		return subtle
	case race.LaneRunning:
		return accent
	case race.LaneDone:
		return green
	case race.LaneFailed:
		return red
	case race.LaneKilled:
		return orange
	default:
		return subtle
	}
}

// RenderRaceView builds the compact lane-pane layout for race mode.
// In comparing phase it renders the selection table or diff view.
func (m Model) RenderRaceView() string {
	if m.Race == nil {
		return "Initializing race..."
	}

	// Post-race comparing/diff/gate phase
	if m._raceComparing {
		if m._raceViewingGate >= 0 {
			return m.renderRaceGateOutputView()
		}
		if m._raceViewingDiff >= 0 {
			return m.renderRaceDiffView()
		}
		return m.renderRaceCompareView()
	}

	paneWidth := m.Width - 4
	if paneWidth < 20 {
		paneWidth = 20
	}

	var panes []string

	for _, l := range m.Race.Lanes {
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

		// Build content lines.
		icon := laneStatusIcon(l.Status)
		color := laneStatusColor(l.Status)
		statusStr := l.Status.String()

		header := lipgloss.NewStyle().Bold(true).Render(
			fmt.Sprintf("Lane %d  %s  %s", l.ID, icon, l.Model))

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
			// Truncate long tail lines to fit in pane.
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
		content.WriteString(header + "\n")
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
			BorderForeground(border).
			Padding(0, 1).
			Width(paneWidth).
			Render(strings.TrimSpace(content.String()))

		panes = append(panes, pane)
	}

	if len(panes) == 0 {
		return "No lanes."
	}

	// Vertical stack with gutter.
	raceContent := lipgloss.JoinVertical(lipgloss.Left, panes...)

	// Wrap everything in the standard layout structure (footer handled by View).
	// Return just the main content; View() wraps it with textarea and footer.
	return raceContent
}

// ── Race Comparing Phase ──────────────────────────────────────────────────

// RaceDiffMsg is returned by computeRaceDiffsCmd with per-lane diffs.
type RaceDiffMsg struct {
	Diffs     map[int]string
	DiffLines map[int]int
}

// RaceGatesMsg is returned by runGatesCmd with pass/fail per lane.
type RaceGatesMsg struct {
	Results map[int]bool
	Outputs map[int]string
}

// RaceMergeResultMsg is returned by mergeLaneCmd with merge outcome.
type RaceMergeResultMsg struct {
	LaneID  int
	Model   string
	Changes int
	Err     error
}

// enterRaceComparing transitions from racing to comparing/merge selection.
// RaceMode stays true; _raceComparing becomes true.
func (m Model) enterRaceComparing() Model {
	if m.Race == nil {
		return m
	}
	m._raceComparing = true
	m._raceHighlight = 0
	m._raceViewingDiff = -1
	m._raceDiffs = make(map[int]string)
	m._raceDiffLines = make(map[int]int)
	m._raceDiffScroll = 0
	m._raceMergePending = false
	m._raceGates = make(map[int]bool)
	m._raceGateOutput = make(map[int]string)
	m._raceGatesRunning = true
	m._raceViewingGate = -1
	m._raceGateScroll = 0
	// Skip to first mergeable lane
	for i, l := range m.Race.Lanes {
		if l != nil && l.Status == race.LaneDone {
			m._raceHighlight = i
			break
		}
	}
	m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
	m.Viewport.GotoBottom()
	return m
}

// discardRace cleans up race dir and exits race mode entirely.
// Caller should add a history message before calling this.
func (m Model) discardRace() Model {
	if m.Race != nil {
		_ = m.Race.Cleanup()
	}
	m.RaceMode = false
	m._raceComparing = false
	m.Race = nil
	m._raceDiffs = nil
	m._raceDiffLines = nil
	m._raceHighlight = 0
	m._raceViewingDiff = -1
	m._raceDiffScroll = 0
	m._raceMergePending = false
	m._raceNoOverview = false
	m._raceGates = nil
	m._raceGateOutput = nil
	m._raceGatesRunning = false
	m._raceViewingGate = -1
	m._raceGateScroll = 0
	m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
	m.Viewport.GotoBottom()
	return m
}

// computeRaceDiffsCmd shells out DiffLane for each done lane in a goroutine
// and returns a single RaceDiffMsg when all are computed.
func computeRaceDiffsCmd(r *race.Race) tea.Cmd {
	return func() tea.Msg {
		diffs := make(map[int]string)
		diffLines := make(map[int]int)
		for _, l := range r.Lanes {
			if l == nil {
				continue
			}
			if l.Status != race.LaneDone {
				diffs[l.ID] = ""
				diffLines[l.ID] = 0
				continue
			}
			d, err := r.DiffLane(l)
			if err != nil {
				// Exit 0 (identical) returns "", nil.
				// Any other error means diff failed — store empty.
				diffs[l.ID] = ""
				diffLines[l.ID] = 0
				continue
			}
			diffs[l.ID] = d
			lines := 0
			if d != "" {
				lines = len(strings.Split(d, "\n"))
			}
			diffLines[l.ID] = lines
		}
		return RaceDiffMsg{Diffs: diffs, DiffLines: diffLines}
	}
}

// runGatesCmd runs DefaultGateCommand for each done lane concurrently and
// returns a single RaceGatesMsg when all gates finish.
func runGatesCmd(r *race.Race) tea.Cmd {
	return func() tea.Msg {
		results := make(map[int]bool)
		outputs := make(map[int]string)
		var mu sync.Mutex
		var wg sync.WaitGroup

		for _, l := range r.Lanes {
			if l == nil {
				continue
			}
			if l.Status != race.LaneDone {
				results[l.ID] = false
				outputs[l.ID] = ""
				continue
			}
			wg.Add(1)
			go func(lane *race.Lane) {
				defer wg.Done()
				cmd := race.DefaultGateCommand(lane.Dir)
				passed, out, _ := r.GateLane(lane, cmd, 120*time.Second)
				mu.Lock()
				results[lane.ID] = passed
				outputs[lane.ID] = out
				mu.Unlock()
			}(l)
		}
		wg.Wait()
		return RaceGatesMsg{Results: results, Outputs: outputs}
	}
}

// mergeLaneCmd shells out the merge for one lane: kills all lanes, verifies
// the marker, constructs a rift workspace, and calls MergeBack.
func mergeLaneCmd(r *race.Race, laneIdx int, sourceDir string, mgr *rift.Manager) tea.Cmd {
	return func() tea.Msg {
		if r == nil {
			return RaceMergeResultMsg{Err: fmt.Errorf("race is nil")}
		}
		if laneIdx < 0 || laneIdx >= len(r.Lanes) {
			return RaceMergeResultMsg{LaneID: laneIdx, Err: fmt.Errorf("invalid lane index %d", laneIdx)}
		}
		l := r.Lanes[laneIdx]
		if l == nil {
			return RaceMergeResultMsg{LaneID: laneIdx, Err: fmt.Errorf("lane %d is nil", laneIdx)}
		}
		if mgr == nil {
			return RaceMergeResultMsg{LaneID: laneIdx, Model: l.Model, Err: fmt.Errorf("workspace manager not available")}
		}

		// Defensive: kill any still-running lanes
		r.KillAll()

		// Verify the lane marker
		if err := race.VerifyLane(l); err != nil {
			return RaceMergeResultMsg{LaneID: laneIdx, Model: l.Model, Err: err}
		}

		// Build workspace and merge back
		ws := &rift.Workspace{
			Root:   l.Dir,
			Source: sourceDir,
		}
		changes, err := mgr.MergeBack(ws)
		if err != nil {
			return RaceMergeResultMsg{LaneID: laneIdx, Model: l.Model, Changes: len(changes), Err: err}
		}
		return RaceMergeResultMsg{LaneID: laneIdx, Model: l.Model, Changes: len(changes)}
	}
}

// ── Comparing View Rendering ──────────────────────────────────────────────

// renderRaceCompareView renders the lane comparison table after all lanes settle,
// including a GATE column showing pass/fail/pending status.
func (m Model) renderRaceCompareView() string {
	if m.Race == nil {
		return ""
	}

	titleStyle := lipgloss.NewStyle().Bold(true).Foreground(accent)
	rowHighlight := lipgloss.NewStyle().Bold(true).Foreground(accent)
	rowNormal := lipgloss.NewStyle().Foreground(fg)
	rowDim := lipgloss.NewStyle().Foreground(subtle)
	statusDone := lipgloss.NewStyle().Foreground(green)
	statusFail := lipgloss.NewStyle().Foreground(red)
	gatePassStyle := lipgloss.NewStyle().Foreground(green)
	gateFailStyle := lipgloss.NewStyle().Foreground(red)
	gatePendingStyle := lipgloss.NewStyle().Foreground(subtle)

	var sb strings.Builder

	sb.WriteString(titleStyle.Render("═══ Race Complete ═══"))
	sb.WriteString("\n\n")

	for _, l := range m.Race.Lanes {
		if l == nil {
			continue
		}

		isMergeable := l.Status == race.LaneDone
		isHighlighted := l.ID == m._raceHighlight

		prefix := "  "
		if isHighlighted {
			prefix = "▶ "
		}

		statusStr := l.Status.String()
		var statusDisplay string
		switch l.Status {
		case race.LaneDone:
			statusDisplay = statusDone.Render(statusStr)
		case race.LaneFailed, race.LaneKilled:
			statusDisplay = statusFail.Render(statusStr)
		default:
			statusDisplay = lipgloss.NewStyle().Foreground(subtle).Render(statusStr)
		}

		// Diff column
		diffStr := "-"
		if lines, ok := m._raceDiffLines[l.ID]; ok && lines > 0 {
			diffStr = fmt.Sprintf("%d lines", lines)
		} else if ok {
			diffStr = "0 lines"
		}

		// Gate column
		gateStr := ""
		if m._raceGatesRunning {
			gateStr = "…"
		} else if gated, ok := m._raceGates[l.ID]; ok {
			if gated {
				gateStr = gatePassStyle.Render("✓")
			} else {
				gateStr = gateFailStyle.Render("✗")
			}
		} else if l.Status == race.LaneFailed || l.Status == race.LaneKilled {
			gateStr = gatePendingStyle.Render("—")
		} else {
			gateStr = gatePendingStyle.Render("…")
		}

		row := fmt.Sprintf("%sLane %d | %s | %s | rounds: %d | diff: %s | gate: %s",
			prefix, l.ID, l.Model, statusDisplay, l.Rounds, diffStr, gateStr)

		switch {
		case isHighlighted && isMergeable:
			sb.WriteString(rowHighlight.Render(row) + "\n")
		case isMergeable:
			sb.WriteString(rowNormal.Render(row) + "\n")
		default:
			sb.WriteString(rowDim.Render(row) + "\n")
		}
	}

	sb.WriteString("\n")
	hint := "[1-4] view diff  [g] view gate output  [m] merge highlighted  [d] discard all"
	if m._raceMergePending {
		hint = "Merging..."
	}
	sb.WriteString(lipgloss.NewStyle().Foreground(subtle).Render(hint))

	return sb.String()
}

// renderRaceDiffView renders the side-by-side diff for a single lane.
func (m Model) renderRaceDiffView() string {
	diffText, ok := m._raceDiffs[m._raceViewingDiff]
	if !ok {
		return diffViewHeader(m._raceViewingDiff) + "\nNo diff available.\n\n" + diffViewFooter(m._raceViewingDiff)
	}
	if diffText == "" {
		return diffViewHeader(m._raceViewingDiff) + "\nNo changes (identical trees).\n\n" + diffViewFooter(m._raceViewingDiff)
	}

	colWidth := (m.Width - 6) / 2
	if colWidth < 20 {
		colWidth = 20
	}
	rendered := renderSideBySide(diffText, colWidth)
	diffLines := strings.Split(rendered, "\n")
	totalLines := len(diffLines)

	availHeight := m.Height - 5 // header + textarea + footer + buffer
	if availHeight < 5 {
		availHeight = 5
	}

	scroll := m._raceDiffScroll
	if scroll >= totalLines-availHeight && totalLines > availHeight {
		scroll = totalLines - availHeight
	}
	if scroll < 0 {
		scroll = 0
	}
	end := scroll + availHeight
	if end > totalLines {
		end = totalLines
	}
	if scroll > end {
		scroll = end
	}

	visible := diffLines[scroll:end]

	header := fmt.Sprintf("═══ Diff for Lane %d (lines %d-%d of %d) ═══\n",
		m._raceViewingDiff, scroll+1, end, totalLines)
	content := header + strings.Join(visible, "\n")
	content += "\n" + diffViewFooter(m._raceViewingDiff)

	return content
}

func diffViewHeader(laneID int) string {
	return fmt.Sprintf("═══ Diff for Lane %d ═══", laneID)
}

func diffViewFooter(laneID int) string {
	return fmt.Sprintf("Press Esc or %d to return", laneID)
}

// renderRaceGateOutputView renders the gate command output for a single lane.
// It reuses the same scroll mechanism as renderRaceDiffView but under _raceGateScroll.
func (m Model) renderRaceGateOutputView() string {
	laneID := m._raceViewingGate
	if laneID < 0 || m.Race == nil || laneID >= len(m.Race.Lanes) {
		return ""
	}

	out, ok := m._raceGateOutput[laneID]
	if !ok {
		out = "(no gate result)"
	}

	lines := strings.Split(out, "\n")
	totalLines := len(lines)

	availHeight := m.Height - 5
	if availHeight < 5 {
		availHeight = 5
	}

	scroll := m._raceGateScroll
	if scroll >= totalLines-availHeight && totalLines > availHeight {
		scroll = totalLines - availHeight
	}
	if scroll < 0 {
		scroll = 0
	}
	end := scroll + availHeight
	if end > totalLines {
		end = totalLines
	}
	if scroll > end {
		scroll = end
	}

	visible := lines[scroll:end]

	passed := false
	if p, ok := m._raceGates[laneID]; ok {
		passed = p
	}

	statusStr := "FAILED"
	statusColor := red
	if passed {
		statusStr = "PASSED"
		statusColor = green
	}

	statusRendered := lipgloss.NewStyle().Foreground(statusColor).Render(statusStr)
	header := fmt.Sprintf("═══ Gate output for Lane %d — %s ═══\n", laneID, statusRendered)
	content := header + strings.Join(visible, "\n")
	content += "\n\nPress Esc to return"

	return content
}
