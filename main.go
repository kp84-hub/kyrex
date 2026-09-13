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
	"strings"
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
		// A Bot workspace is durable by design and will look stale whenever the
		// Bot has been idle. Sweeping it would delete the Bot's world.
		if rift.IsPersistent(path) {
			continue
		}
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

	// ── kx serve: headless host, no TUI ──
	if len(os.Args) > 1 && os.Args[1] == "serve" {
		runServe()
		return
	}

	// ── kx bot create: persistent workspace for a Bot ──
	if len(os.Args) > 2 && os.Args[1] == "bot" && os.Args[2] == "create" {
		runBotCreate()
		return
	}

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
	// Clean exit path. Signal and error paths call quitCleanup directly,
	// since os.Exit skips deferred functions. In daemon mode the background
	// engine may still be working inside the clone, so the clone outlives
	// the TUI and the startup sweep reaps it long after the daemon exits.
	daemonMode := false
	quitCleanup := func() {
		if daemonMode {
			return
		}
		discardWorkspace(mgr, ws)
	}
	defer quitCleanup()

	// Engine startup, daemon-first: attach to (or spawn) a background engine
	// that survives this app closing, so quitting kx — or losing the terminal
	// entirely — never kills an in-flight turn. The classic child transport
	// stays as the fallback for environments where daemon spawning is not
	// possible (it dies with the app, as before).
	bundledEngine := filepath.Join(workspaceRoot, "kyrex-engine")
	pythonPath := "python3"
	bridgeScript := filepath.Join(kyrexRoot(), "kyrex_engine", "core_bridge.py")
	childArgs := append([]string{bridgeScript}, os.Args[1:]...)

	server, daemonWorkspace, daemonErr := kyrex_engine.AttachOrSpawnDaemon(
		bundledEngine, pythonPath, childArgs, ws.Root, ws.Source)
	if daemonErr != nil {
		// Fallback: classic child process.
		if _, statErr := os.Stat(bundledEngine); statErr == nil {
			server, err = kyrex_engine.NewServerDirect(bundledEngine, ws.Root, ws.Source)
		} else {
			server, err = kyrex_engine.NewServer(pythonPath, childArgs, ws.Root, ws.Source)
		}
		if err != nil {
			discardWorkspace(mgr, ws)
			fmt.Printf("Error starting engine: %v\n", err)
			os.Exit(1)
		}
	} else {
		daemonMode = true
		// The daemon owns its own working directory: a rift clone from the
		// previous run (clone roots change per run), or the project root
		// itself when the IDE spawned the daemon. Adopt it so merge/sweep
		// operate on the tree the engine is actually editing, and drop the
		// fresh clone created above if it turned out to be unused.
		if kyrex_engine.DaemonKey(daemonWorkspace) != kyrex_engine.DaemonKey(ws.Root) {
			if ws.Root != ws.Source {
				_ = mgr.Discard(ws)
			}
			ws = &rift.Workspace{Root: daemonWorkspace, Source: ws.Source}
		}
	}
	defer server.Close()

	if !daemonMode {
		// Pipe stderr to a log file (only for long-running engine sessions).
		// Daemon mode has no stderr pipe — the detached engine logs to its
		// own file in TempDir instead.
		go func() {
			logDir := filepath.Join(os.Getenv("HOME"), ".kx")
			os.MkdirAll(logDir, 0755)
			logFile, _ := os.OpenFile(filepath.Join(logDir, "stderr.log"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0644)
			if logFile != nil {
				defer logFile.Close()
				if r := server.GetStderr(); r != nil {
					io.Copy(logFile, r)
				}
			}
		}()
	}

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
	if daemonMode {
		m.Toast = "Background mode: closing kx won't stop the engine"
		m.ToastEnd = time.Now().Add(8 * time.Second)
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
			// Daemon reattach: the background engine replays everything the
			// TUI missed while the app was closed. Replayed approval gates
			// were already resolved by the daemon's background policy (edits
			// auto-approved, deletions denied) — surface them as history,
			// never as a live modal.
			if msg.Replay && (msg.Type == "confirm_request" || msg.Type == "propose_edit") {
				msg.Type = "log"
				msg.Content = fmt.Sprintf(
					"[replay] %s for %s — already resolved in the background (edits auto-approved, deletions denied).",
					msg.Type, msg.Path)
			}
			if msg.Type == "session_replay" {
				msg.Type = "log"
				if msg.Count > 0 {
					msg.Content = fmt.Sprintf(
						"Reattached to the background engine — replaying %d event(s) from while you were away.",
						msg.Count)
				} else {
					msg.Content = "Reattached to the background engine."
				}
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

		// Second signal within 3 seconds → hard exit. quitCleanup respects
		// daemon mode: a background engine keeps its workspace.
		select {
		case <-sigCh:
			disableMouseTracking()
			quitCleanup()
			os.Exit(1)
		case <-time.After(3 * time.Second):
			// Grace period expired — force exit
			disableMouseTracking()
			quitCleanup()
			os.Exit(1)
		}
	}()

	finalModel, err := p.Run()
	if err != nil {
		disableMouseTracking()
		quitCleanup()
		fmt.Printf("Alas, there's been an error: %v", err)
		os.Exit(1)
	}

	// Write render metrics report on clean exit
	if km, ok := finalModel.(tui.Model); ok {
		km.WriteMetricsReport("/tmp/kyrex_render_metrics.txt")
	}
}

// kyrexRoot resolves the repository root. KYREX_ROOT wins; otherwise fall
// back to $HOME/kyrex, which is what the rest of main.go assumes. That
// assumption is wrong for any checkout not at that path — this at least
// makes it overridable instead of silently failing.
func kyrexRoot() string {
	if r := os.Getenv("KYREX_ROOT"); r != "" {
		return r
	}
	return filepath.Join(os.Getenv("HOME"), "kyrex")
}

// runServe starts the headless host: the same engine and executor routing
// the TUI uses, driven by a chat transport instead of a terminal.
//
// Missing configuration is reported here rather than as a Python
// traceback from a KeyError three frames deep.
func runServe() {
	root := kyrexRoot()
	host := filepath.Join(root, "kyrex-cloud", "telegram_bot.py")
	if _, err := os.Stat(host); err != nil {
		fmt.Fprintf(os.Stderr,
			"kx serve: cannot find %s\n"+
				"Set KYREX_ROOT to your kyrex checkout.\n", host)
		os.Exit(1)
	}

	missing := []string{}
	for _, k := range []string{"TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_ID"} {
		if os.Getenv(k) == "" {
			missing = append(missing, k)
		}
	}
	if len(missing) > 0 {
		fmt.Fprintf(os.Stderr,
			"kx serve: missing required environment: %s\n",
			strings.Join(missing, ", "))
		os.Exit(1)
	}
	if os.Getenv("MCP_SERVERS_JSON") == "" {
		fmt.Fprintln(os.Stderr,
			"kx serve: MCP_SERVERS_JSON unset - starting with no MCP servers.")
	}

	fmt.Fprintf(os.Stderr, "kx serve: starting headless host from %s\n", root)
	cmd := exec.Command("python3", host)
	cmd.Dir = root
	cmd.Env = os.Environ()
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		fmt.Fprintf(os.Stderr, "kx serve: host exited: %v\n", err)
		os.Exit(1)
	}
}

// runBotCreate creates a persistent rift workspace for a Bot.
// Usage: kx bot create <id> <source-path>
// Prints the workspace root path to stdout so the Python caller can record it.
func runBotCreate() {
	// Args are: kx bot create <id> <source-path>
	//           0  1   2      3    4
	if len(os.Args) < 5 {
		fmt.Fprintln(os.Stderr, "kx bot create: usage: kx bot create <id> <source-path>")
		os.Exit(1)
	}
	id := os.Args[3]
	sourcePath := os.Args[4]

	fi, err := os.Stat(sourcePath)
	if err != nil || !fi.IsDir() {
		fmt.Fprintf(os.Stderr, "kx bot create: source path %q does not exist or is not a directory\n", sourcePath)
		os.Exit(1)
	}

	mgr := rift.New()
	ws, err := mgr.Create(sourcePath, id)
	if err != nil {
		fmt.Fprintf(os.Stderr, "kx bot create: %v\n", err)
		os.Exit(1)
	}

	if err := rift.MarkPersistent(ws); err != nil {
		// Clean up the unmarked clone so we don't leave orphaned workspaces.
		_ = mgr.Destroy(ws)
		fmt.Fprintf(os.Stderr, "kx bot create: marking persistent: %v\n", err)
		os.Exit(1)
	}

	fmt.Println(ws.Root)
}
