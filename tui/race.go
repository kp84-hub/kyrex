package tui

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/charmbracelet/lipgloss"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/kp84-hub/kx/internal/race"
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
func (m Model) RenderRaceView() string {
	if m.Race == nil {
		return "Initializing race..."
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
