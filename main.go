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
	"syscall"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/kp84-hub/kx/kyrex_engine"
	"github.com/kp84-hub/kx/tui"
)

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
	os.Stdout.WriteString("\x1b[?1006l\x1b[?1015l\x1b[?1003l\x1b[?1000l")
	os.Stdout.Sync()
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
	hasSetupOrPrint := false
	for _, arg := range os.Args[1:] {
		if arg == "--setup" || arg == "-p" {
			hasSetupOrPrint = true
			break
		}
	}
	if hasSetupOrPrint {
		pythonPath := "python3"
		bridgeScript := filepath.Join(workspaceRoot, "kyrex_engine", "core_bridge.py")
		cmdArgs := append([]string{bridgeScript}, os.Args[1:]...)
		cmd := exec.Command(pythonPath, cmdArgs...)
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
	homeConfig := filepath.Join(os.Getenv("HOME"), ".px", "config.json")
	if _, err := os.Stat(homeConfig); os.IsNotExist(err) {
		printWelcomeAndExit()
	}

	// Try bundled kyrex-engine binary first, fall back to Python bridge
	bundledEngine := filepath.Join(workspaceRoot, "kyrex-engine")
	var server *kyrex_engine.Server

	if _, statErr := os.Stat(bundledEngine); statErr == nil {
		server, err = kyrex_engine.NewServerDirect(bundledEngine)
	} else {
		pythonPath := "python3"
                bridgeScript := filepath.Join(filepath.Dir(exe), "kyrex_engine", "core_bridge.py")
		// Pass bridge script and all OS arguments
		args := append([]string{bridgeScript}, os.Args[1:]...)
		server, err = kyrex_engine.NewServer(pythonPath, args...)
	}
	if err != nil {
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
	p := tea.NewProgram(m, tea.WithAltScreen(), tea.WithMouseCellMotion())

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
			Type:      msg.Type,
			ID:        msg.ID,
			Content:   content,
			Phase:     tui.Phase(msg.Value),
			Name:      msg.Name,
			Args:      msg.Args,
			Result:    msg.Result,
			Value:     msg.Value,
			Model:     msg.Model,
			Provider:  msg.Provider,
			Context:   msg.Context,
			Files:     msg.Files,
			Stdout:    msg.Stdout,
			Reasoning: msg.Reasoning,
			Todos:     msg.Todos,
			RequestID: msg.ID,
			Path:      msg.Path,
			Diff:      msg.Diff,
			SessionBranch: msg.Branch,
			Mode:          msg.Mode,
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
			os.Exit(1)
		case <-time.After(3 * time.Second):
			// Grace period expired — force exit
			disableMouseTracking()
			os.Exit(1)
		}
	}()

	finalModel, err := p.Run()
	if err != nil {
		disableMouseTracking()
		fmt.Printf("Alas, there's been an error: %v", err)
		os.Exit(1)
	}

	// Write render metrics report on clean exit
	if km, ok := finalModel.(tui.Model); ok {
		km.WriteMetricsReport("/tmp/kyrex_render_metrics.txt")
	}
}
