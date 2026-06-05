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

	tea "github.com/charmbracelet/bubbletea"
	"github.com/kp84-hub/kx/kyrex_engine"
	"github.com/kp84-hub/kx/tui"
)

// disableMouseTracking sends the ANSI escape to turn off any active
// mouse tracking mode so escape codes don't leak into the terminal.
func disableMouseTracking() {
	// Disable all mouse tracking modes so escape codes don't leak into the terminal
	os.Stdout.WriteString("\x1b[?1006l\x1b[?1015l\x1b[?1003l\x1b[?1000l")
	os.Stdout.Sync()
}

func main() {
	// If --setup flag passed, bypass TUI and run setup wizard directly in terminal
	for _, arg := range os.Args[1:] {
		if arg == "--setup" || arg == "-p" {
			exe, _ := os.Executable()
			workspaceRoot := filepath.Dir(exe)
			pythonPath := filepath.Join(workspaceRoot, "venv", "bin", "python3")
			bridgeScript := filepath.Join(workspaceRoot, "kyrex_engine", "core_bridge.py")
			cmdArgs := append([]string{bridgeScript}, os.Args[1:]...)
			cmd := exec.Command(pythonPath, cmdArgs...)
			cmd.Stdin = os.Stdin
			cmd.Stdout = os.Stdout
			cmd.Stderr = os.Stderr
			cmd.Run()
			return
		}
	}

	// Anchor paths relative to the binary so Kyrex finds its engine regardless of
	// where it's invoked from. Workspace context follows os.Getwd() at runtime.
	exe, err := os.Executable()
	if err != nil {
		fmt.Printf("Error locating binary: %v\n", err)
		os.Exit(1)
	}
	workspaceRoot := filepath.Dir(exe)

	// Try bundled kyrex-engine binary first, fall back to Python bridge
	bundledEngine := filepath.Join(workspaceRoot, "kyrex-engine")
	var server *kyrex_engine.Server

	if _, statErr := os.Stat(bundledEngine); statErr == nil {
		server, err = kyrex_engine.NewServerDirect(bundledEngine)
	} else {
		pythonPath := filepath.Join(workspaceRoot, "venv", "bin", "python3")
		bridgeScript := filepath.Join(workspaceRoot, "kyrex_engine", "core_bridge.py")
		// Pass bridge script and all OS arguments
		args := append([]string{bridgeScript}, os.Args[1:]...)
		server, err = kyrex_engine.NewServer(pythonPath, args...)
	}
	if err != nil {
		fmt.Printf("Error starting engine: %v\n", err)
		os.Exit(1)
	}
	defer server.Close()

	// Pipe stderr to a log file
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
	p := tea.NewProgram(m, tea.WithAltScreen(), tea.WithMouseAllMotion())

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

	// Ensure mouse tracking is disabled on interrupt/terminate so escape
	// codes don't leak into the shell prompt on WSL / Windows Terminal.
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-sigCh
		disableMouseTracking()
		os.Exit(0)
	}()

	if _, err := p.Run(); err != nil {
		disableMouseTracking()
		fmt.Printf("Alas, there's been an error: %v", err)
		os.Exit(1)
	}
}
