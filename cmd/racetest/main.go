// Command racetest is a headless CLI harness for Kyrex race mode.
// It proves the internal/race package end-to-end: clone, spawn, event loop,
// diff, and cleanup.
package main

import (
	"flag"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/kp84-hub/kx/internal/race"
)

func main() {
	var (
		src    string
		task   string
		models string
		engine string
		keep   bool
	)
	flag.StringVar(&src, "src", "", "source workspace directory (required)")
	flag.StringVar(&task, "task", "", "prompt string for the models (required)")
	flag.StringVar(&models, "models", "", "comma-separated model IDs (required)")
	flag.StringVar(&engine, "engine", "", "engine command as one string, split on whitespace (required)")
	flag.BoolVar(&keep, "keep", false, "preserve the race directory after completion for inspecting diffs")
	flag.Parse()

	if src == "" || task == "" || models == "" || engine == "" {
		fmt.Fprintf(os.Stderr, "Error: -src, -task, -models, and -engine are all required\n")
		flag.Usage()
		os.Exit(1)
	}

	// Validate source directory exists.
	if fi, err := os.Stat(src); err != nil || !fi.IsDir() {
		fmt.Fprintf(os.Stderr, "Error: -src %s is not a valid directory\n", src)
		os.Exit(1)
	}

	modelList := strings.Split(models, ",")
	if len(modelList) == 0 {
		fmt.Fprintf(os.Stderr, "Error: -models produced empty list\n")
		os.Exit(1)
	}

	engineCmd := strings.Fields(engine)
	if len(engineCmd) == 0 {
		fmt.Fprintf(os.Stderr, "Error: -engine produced empty command after splitting\n")
		os.Exit(1)
	}

	homeDir, err := os.UserHomeDir()
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error: cannot determine home directory: %v\n", err)
		os.Exit(1)
	}

	racesDir := filepath.Join(homeDir, ".kx", "races")

	// ── (1) Sweep abandoned races ──────────────────────────────────────
	abandoned, err := race.FindAbandoned(racesDir)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Warning: FindAbandoned error: %v\n", err)
	}
	for _, m := range abandoned {
		fmt.Printf("Removing abandoned race (started %s): %s\n",
			m.StartedAt.Format(time.RFC3339), m.RaceDir)
		if err := os.RemoveAll(m.RaceDir); err != nil {
			fmt.Fprintf(os.Stderr, "Warning: failed to remove %s: %v\n", m.RaceDir, err)
		}
	}

	// ── (2) Create race directory ─────────────────────────────────────
	raceDir := filepath.Join(racesDir, fmt.Sprintf("race-%d", time.Now().Unix()))
	if err := os.MkdirAll(raceDir, 0755); err != nil {
		fmt.Fprintf(os.Stderr, "Error: mkdir %s: %v\n", raceDir, err)
		os.Exit(1)
	}
	fmt.Printf("Race directory: %s\n", raceDir)

	// ── (3) Create Race (clone all lanes) ─────────────────────────────
	r, err := race.New(task, src, raceDir, modelList)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error: race.New: %v\n", err)
		os.RemoveAll(raceDir)
		os.Exit(1)
	}

	var cloneTotal float64
	for i, secs := range r.CloneSecs {
		fmt.Printf("  Lane %d clone: %.2fs\n", i, secs)
		cloneTotal += secs
	}
	fmt.Printf("Total cloning wall time: %.2fs\n", cloneTotal)

	// ── (4) Signal handler ────────────────────────────────────────────
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-sigCh
		fmt.Println("\nInterrupt received — cleaning up race...")
		_ = r.Cleanup()
		os.Exit(130)
	}()

	// ── (5) Spawn, StartReader, SendLine for each lane ────────────────
	msgCh := make(chan any, 256)

	for _, l := range r.Lanes {
		if l == nil {
			continue
		}
		if err := l.Spawn(engineCmd, raceDir); err != nil {
			fmt.Fprintf(os.Stderr, "Error: lane %d Spawn: %v\n", l.ID, err)
			r.Cleanup()
			os.Exit(1)
		}
		if err := l.StartReader(func(msg any) { msgCh <- msg }); err != nil {
			fmt.Fprintf(os.Stderr, "Error: lane %d StartReader: %v\n", l.ID, err)
			r.Cleanup()
			os.Exit(1)
		}
		if err := l.SendLine(map[string]any{"type": "chat", "content": task}); err != nil {
			fmt.Fprintf(os.Stderr, "Error: lane %d SendLine: %v\n", l.ID, err)
			r.Cleanup()
			os.Exit(1)
		}
		fmt.Printf("Started lane %d: model=%s dir=%s\n", l.ID, l.Model, l.Dir)
	}

	// ── (6) Write manifest ────────────────────────────────────────────
	if err := r.WriteManifest(); err != nil {
		fmt.Fprintf(os.Stderr, "Warning: WriteManifest: %v\n", err)
	}

	// ── (7) Event loop ────────────────────────────────────────────────
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()

	roundCap := r.RoundCap

	for !r.AllSettled() {
		select {
		case msg := <-msgCh:
			switch m := msg.(type) {
			case race.LaneMsg:
				l := r.Lanes[m.LaneID]
				if l == nil {
					continue
				}
				ev := m.Event
				switch {
				case ev.IsTool():
					l.Rounds++
					l.LastTool = ev.Name
					fmt.Printf("[lane %d] round %d: %s\n", l.ID, l.Rounds, l.LastTool)
				case ev.IsDone():
					l.Status = race.LaneDone
					l.FinishedAt = time.Now()
					fmt.Printf("[lane %d] done\n", l.ID)
				case ev.IsError():
					l.Status = race.LaneFailed
					l.Err = ev.ErrText()
					l.FinishedAt = time.Now()
					fmt.Printf("[lane %d] error: %s\n", l.ID, l.Err)
				default:
					// Ignore token/streaming and other events — too noisy.
				}

			case race.LaneExitMsg:
				l := r.Lanes[m.LaneID]
				if l == nil {
					continue
				}
				// If lane is still running when the stream exits, it crashed.
				if l.Status == race.LaneRunning || l.Status == race.LanePending {
					l.Status = race.LaneFailed
					if m.Err != nil {
						l.Err = m.Err.Error()
					} else {
						l.Err = "unexpected exit"
					}
					l.FinishedAt = time.Now()
					logPath := filepath.Join(raceDir, fmt.Sprintf("lane-%d.stderr.log", l.ID))
					fmt.Printf("[lane %d] crashed: %s (see %s)\n", l.ID, l.Err, logPath)
				}
			}

		case <-ticker.C:
			for _, l := range r.Lanes {
				if l == nil {
					continue
				}
				if l.Status == race.LaneRunning && l.Rounds > roundCap {
					fmt.Printf("[lane %d] killing — exceeded round cap (%d)\n", l.ID, roundCap)
					l.Kill()
				}
			}
		}
	}

	// ── (8) Results table ─────────────────────────────────────────────
	fmt.Println("\n═══ RESULTS ═══")
	for _, l := range r.Lanes {
		if l == nil {
			continue
		}
		if err := race.VerifyLane(l); err != nil {
			fmt.Fprintf(os.Stderr, "  Lane %d: VerifyLane: %v — skipping diff\n", l.ID, err)
			continue
		}
		diff, diffErr := r.DiffLane(l)
		diffPath := ""
		diffLines := 0
		if diffErr == nil {
			if diff != "" {
				diffLines = strings.Count(diff, "\n")
				diffPath = filepath.Join(raceDir, fmt.Sprintf("lane-%d.diff", l.ID))
				if err := os.WriteFile(diffPath, []byte(diff), 0644); err != nil {
					fmt.Fprintf(os.Stderr, "  Lane %d: write diff: %v\n", l.ID, err)
					diffPath = ""
				}
			}
		} else {
			fmt.Fprintf(os.Stderr, "  Lane %d: DiffLane error: %v\n", l.ID, diffErr)
		}
		fmt.Printf("  Lane %d | model=%s | status=%s | rounds=%d | diff_lines=%d | diff=%s\n",
			l.ID, l.Model, l.Status, l.Rounds, diffLines, diffPath)
	}

	// ── (9) Cleanup ───────────────────────────────────────────────────
	if keep {
		fmt.Printf("Preserving race directory: %s (-keep)\n", raceDir)
	} else {
		if err := r.Cleanup(); err != nil {
			fmt.Fprintf(os.Stderr, "Warning: Cleanup error: %v\n", err)
		} else {
			fmt.Println("Cleanup complete: race directory removed")
		}
	}
}
