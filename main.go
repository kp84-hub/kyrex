// 🚀 TUI Render Verification: Success!
// 💻 Test UTF-8 / CJK Cell Alignment: ⚡ 【凯雷克斯】 ⚡
// ─── End of Line-Matching Test ───

package main

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/kp84-hub/kx/internal/rift"
	"github.com/kp84-hub/kx/kyrex_engine"
	"github.com/kp84-hub/kx/tui"
)

// riftMaxAge is how long an orphaned clone may survive before the startup
// sweep removes it.
const riftMaxAge = 24 * time.Hour

// printWelcomeAndExit prints a branded welcome screen and exits.
// Called when no config file is found before spawning the engine.
func printWelcomeAndExit() {
	C := "\033[96m"
	W := "\033[97m"
	N := "\033[0m"
	fmt.Println()
	fmt.Printf("  %s+------------------------------------------------+%s\n", C, N)
	fmt.Printf("  %s|%s                                                %s|%s\n", C, W, C, N)
	fmt.Printf("  %s|%s          K   Y   R   E   X                     %s|%s\n", C, W, C, N)
	fmt.Printf("  %s|%s          Terminal AI Agent                      %s|%s\n", C, W, C, N)
	fmt.Printf("  %s|%s                                                %s|%s\n", C, W, C, N)
	fmt.Printf("  %s+------------------------------------------------+%s\n", C, N)
	fmt.Println()
	fmt.Printf("  %sKyrex needs to be configured before first use.%s\n", W, N)
	fmt.Printf("  %sRun the setup wizard to connect to an AI provider:%s\n", W, N)
	fmt.Println()
	fmt.Printf("    %skx --setup%s\n", C, N)
	fmt.Println()
	fmt.Printf("  %sThe wizard will guide you through:%s\n", W, N)
	fmt.Printf("  %s  - Choosing a provider (OpenAI-compatible or Anthropic)%s\n", W, N)
	fmt.Printf("  %s  - Setting your API key or environment variable%s\n", W, N)
	fmt.Printf("  %s  - Selecting a model from available options%s\n", W, N)
	fmt.Printf("  %s  - Testing the connection%s\n", W, N)
	fmt.Println()
	os.Exit(0)
}

// disableMouseTracking sends the ANSI escape to turn off any active
// mouse tracking mode so escape codes don't leak into the terminal.
func disableMouseTracking() {
	// Disable all mouse tracking modes so escape codes don't leak into the terminal
	os.Stdout.WriteString("\x1b[?1006l\x1b[?1002l\x1b[?1015l\x1b[?1003l\x1b[?1000l")
	os.Stdout.Sync()
}

func runUpdate() {
	home, err := os.UserHomeDir()
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error: cannot determine home directory: %v\n", err)
		os.Exit(1)
	}

	repoDir := filepath.Join(home, "kyrex")
	if info, err := os.Stat(repoDir); err != nil || !info.IsDir() {
		fmt.Fprintf(os.Stderr, "Error: Kyrex repo not found at %s\n", repoDir)
		os.Exit(1)
	}

	binDir := filepath.Join(home, ".local", "bin")
	outBin := filepath.Join(binDir, "kx")
	if err := os.MkdirAll(binDir, 0755); err != nil {
		fmt.Fprintf(os.Stderr, "Error: cannot create %s: %v\n", binDir, err)
		os.Exit(1)
	}

	fmt.Println("Pulling latest changes...")
	cmd := exec.Command("git", "pull")
	cmd.Dir = repoDir
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		fmt.Fprintf(os.Stderr, "Update failed during 'git pull': %v\n", err)
		os.Exit(1)
	}

	fmt.Println("Installing Python engine...")
	cmd = exec.Command("pip", "install", "-e", "kyrex_engine/", "--break-system-packages", "--quiet")
	cmd.Dir = repoDir
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		fmt.Fprintf(os.Stderr, "Update failed during 'pip install': %v\n", err)
		os.Exit(1)
	}

	fmt.Println("Building kx binary...")
	cmd = exec.Command("go", "build", "-o", outBin, ".")
	cmd.Dir = repoDir
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		fmt.Fprintf(os.Stderr, "Update failed during 'go build': %v\n", err)
		os.Exit(1)
	}

	fmt.Printf("Kyrex updated successfully. New binary: %s\n", outBin)
}

// cleanupOnce guards workspace discard so it can run from the signal handler,
// the error paths, and the deferred call without double-discarding.
var cleanupOnce sync.Once

// discardWorkspace removes the rift clone. Safe to call from any exit path,
// including ones that end in os.Exit (which skips deferred functions).
func discardWorkspace(mgr *rift.Manager, ws *rift.Workspace) {
	cleanupOnce.Do(func() {
		if mgr != nil && ws != nil && ws.Root != ws.Source {
			_ = mgr.Discard(ws)
		}
	})
}

// sweepStaleRifts deletes clone directories older than maxAge. Signal handling
// covers SIGINT/SIGTERM, but SIGKILL, a render-loop panic, a closed terminal,
// or WSL shutting down cannot be trapped — and each strands a full copy of the
// repository. Without this sweep they accumulate unboundedly.
func sweepStaleRifts(storage string, maxAge time.Duration) (removed int, freed int64) {
	entries, err := os.ReadDir(storage)
	if err != nil {
		return 0, 0
	}
	cutoff := time.Now().Add(-maxAge)
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		info, err := e.Info()
		if err != nil || info.ModTime().After(cutoff) {
			continue
		}
		path := filepath.Join(storage, e.Name())
		size := dirSize(path)
		if err := os.RemoveAll(path); err == nil {
			removed++
			freed += size
		}
	}
	return removed, freed
}

// dirSize best-effort sums a directory's file sizes; errors are skipped since
// this only feeds a human-readable message.
func dirSize(path string) int64 {
	var total int64
	_ = filepath.Walk(path, func(_ string, info os.FileInfo, err error) error {
		if err == nil && info != nil && !info.IsDir() {
			total += info.Size()
		}
		return nil
	})
	return total
}

func main() {
	// Anchor paths relative to the binary so Kyrex finds its engine regardless of
	// where it's invoked from. Workspace context follows os.Getwd() at runtime.
	exe, err := os.Executable()
	if err != nil {
		fmt.Printf("Error locating binary: %v\n", err)
		os.Exit(1)
	}
	workspaceRoot := filepath.Dir(exe)

	// ── Check for flag-only modes (bypass config check + TUI) ──
	for _, arg := range os.Args[1:] {
		if arg == "--update" {
			runUpdate()
			return
		}
	}
	hasSetupOrPrint := false
	for _, arg := range os.Args[1:] {
		if arg == "--setup" || arg == "-p" {
			hasSetupOrPrint = true
			break
		}
	}
	if hasSetupOrPrint {
		pythonPath := "python3"
		bridgeScript := filepath.Join(os.Getenv("HOME"), "kyrex", "kyrex_engine", "core_bridge.py")
		cmdArgs := append([]string{bridgeScript}, os.Args[1:]...)
		cmd := exec.Command(pythonPath, cmdArgs...)
		cmd.Env = append(os.Environ(), "KYREX_SURFACE=terminal")
		cmd.Stdin = os.Stdin
		cmd.Stdout = os.Stdout
		cmd.Stderr = os.Stderr
		if err := cmd.Run(); err != nil {
			fmt.Fprintf(os.Stderr, "Engine error: %v\n", err)
			os.Exit(1)
		}
		return
	}

	// ── Config check before spawning engine ──
	// Check for config in project-local .px/ first, then fall back to HOME
	projectConfig := filepath.Join(".px", "config.json")
	homeConfig := filepath.Join(os.Getenv("HOME"), ".px", "config.json")
	if _, err := os.Stat(projectConfig); os.IsNotExist(err) {
		if _, err := os.Stat(homeConfig); os.IsNotExist(err) {
			printWelcomeAndExit()
		}
	}

	// Determine the project source root (where the user ran kx from)
	projectSourceRoot, err := os.Getwd()
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error getting working directory: %v\n", err)
		os.Exit(1)
	}

	// Create a copy-on-write workspace so the engine edits an isolated clone
	mgr := rift.New()

	// Prune clones stranded by untrappable exits before adding another.
	if removed, freed := sweepStaleRifts(
		filepath.Join(filepath.Dir(projectSourceRoot), ".rifts", filepath.Base(projectSourceRoot)),
		riftMaxAge,
	); removed > 0 {
		fmt.Fprintf(os.Stderr, "rift: swept %d stale workspace(s), freed %.1f MB\n",
			removed, float64(freed)/(1024*1024))
	}

	// Without this line a slow clone is indistinguishable from a hang.
	fmt.Fprint(os.Stderr, "preparing workspace…")
	cloneStart := time.Now()
	ws, wsErr := mgr.Create(projectSourceRoot, "")
	fmt.Fprintf(os.Stderr, " done (%.1fs)\n", time.Since(cloneStart).Seconds())
	if wsErr != nil {
		fmt.Fprintf(os.Stderr, "rift: clone failed, using live project: %v\n", wsErr)
		ws = &rift.Workspace{Root: projectSourceRoot, Source: projectSourceRoot}
	}
	// Clean exit path. Signal and error paths call discardWorkspace directly,
	// since os.Exit skips deferred functions.
	defer discardWorkspace(mgr, ws)

	// Try bundled kyrex-engine binary first, fall back to Python bridge
	bundledEngine := filepath.Join(workspaceRoot, "kyrex-engine")
	var server *kyrex_engine.Server

	if _, statErr := os.Stat(bundledEngine); statErr == nil {
		server, err = kyrex_engine.NewServerDirect(bundledEngine, ws.Root, ws.Source)
	} else {
		pythonPath := "python3"
		bridgeScript := filepath.Join(os.Getenv("HOME"), "kyrex", "kyrex_engine", "core_bridge.py")
		// Pass bridge script and all OS arguments
		args := append([]string{bridgeScript}, os.Args[1:]...)
		server, err = kyrex_engine.NewServer(pythonPath, args, ws.Root, ws.Source)
	}
	if err != nil {
		discardWorkspace(mgr, ws)
		fmt.Printf("Error starting engine: %v\n", err)
		os.Exit(1)
	}
	defer server.Close()

	// Pipe stderr to a log file (only for long-running engine sessions)
	go func() {
		logDir := filepath.Join(os.Getenv("HOME"), ".kx")
		os.MkdirAll(logDir, 0755)
		logFile, _ := os.OpenFile(filepath.Join(logDir, "stderr.log"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0644)
		if logFile != nil {
			defer logFile.Close()
			io.Copy(logFile, server.GetStderr())
		}
	}()

	m := tui.NewModel(server.Send)
	m.Workspace = ws
	m.WorkspaceMgr = mgr
	// Anything already dirty at clone time is the operator's own work, not
	// something the agent did. Record it so the turn-end sweep can report
	// only what appeared during the session.
	if ws.Root != ws.Source {
		if base, err := mgr.Changes(ws); err == nil {
			m.SweepBaseline = make(map[string]bool, len(base))
			for _, c := range base {
				m.SweepBaseline[c.Path] = true
			}
		}
	}
	if ws.Root == ws.Source {
		m.Toast = "⚠ No clone — editing live project tree"
		m.ToastEnd = time.Now().Add(10 * time.Second)
	}
	p := tea.NewProgram(m, tea.WithAltScreen(), tea.WithMouseCellMotion())
	tui.Program = p

	// Start a goroutine to read from the engine and send messages to the TUI
	go func() {
		for {
			msg, err := server.Next()
			if err != nil {
				// Handle EOF or error
				break
			}
			content := msg.Content
			if content == "" && msg.Result != nil {
				if s, ok := msg.Result.(string); ok && s != "" {
					content = s
				}
			}

			p.Send(tui.MsgFromEngine{
				Type:          msg.Type,
				ID:            msg.ID,
				Content:       content,
				Phase:         tui.Phase(msg.Value),
				Name:          msg.Name,
				Args:          msg.Args,
				Result:        msg.Result,
				Value:         msg.Value,
				Model:         msg.Model,
				Provider:      msg.Provider,
				Context:       msg.Context,
				Files:         msg.Files,
				Stdout:        msg.Stdout,
				Reasoning:     msg.Reasoning,
				Todos:         msg.Todos,
				RequestID:     msg.ID,
				Path:          msg.Path,
				Diff:          msg.Diff,
				SessionBranch: msg.Branch,
			})
		}
	}()

	// Start a goroutine to handle TUI-to-Engine communication
	// This would need a way to receive messages from the update loop
	// For now, we'll just handle the chat trigger in the Update loop directly
	// by returning a command that calls server.Send

	// Graceful shutdown: SIGINT → quit TUI → engine Close() handles SIGTERM→SIGKILL.
	// First signal quits cleanly; second signal within 3s forces immediate exit.
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-sigCh
		disableMouseTracking()
		p.Quit()

		// Second signal within 3 seconds → hard exit
		select {
		case <-sigCh:
			disableMouseTracking()
			discardWorkspace(mgr, ws)
			os.Exit(1)
		case <-time.After(3 * time.Second):
			// Grace period expired — force exit
			disableMouseTracking()
			discardWorkspace(mgr, ws)
			os.Exit(1)
		}
	}()

	finalModel, err := p.Run()
	if err != nil {
		disableMouseTracking()
		discardWorkspace(mgr, ws)
		fmt.Printf("Alas, there's been an error: %v", err)
		os.Exit(1)
	}

	// Write render metrics report on clean exit
	if km, ok := finalModel.(tui.Model); ok {
		km.WriteMetricsReport("/tmp/kyrex_render_metrics.txt")
	}
}
