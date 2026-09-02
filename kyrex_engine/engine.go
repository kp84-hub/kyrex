package kyrex_engine

import (
	"bufio"
	"encoding/json"
	"io"
        "os"
	"os/exec"
	"strings"
	"syscall"
	"time"
)

// Message matches the complete IPC data interface required by main.go and tui
type Message struct {
	Type      string      `json:"type"`
	ID        string      `json:"id"`
	Content   string      `json:"content"`
	Value     string      `json:"value"`
	Name      string      `json:"name"`
	Args      interface{} `json:"args"`   // Restored to interface{} for main.go lines 62-63
	Result    interface{} `json:"result"` // Restored to interface{} for main.go lines 62-63
	Model     string      `json:"model"`
	Provider  string      `json:"provider"`
	Context   string      `json:"context"`
	Files     interface{} `json:"files"`
	Stdout    string      `json:"stdout"`
	Reasoning string      `json:"reasoning"`
	Todos     []string    `json:"todos"`
	Path      string      `json:"path"`
	Paths     []string    `json:"paths"`
	Diff      string      `json:"diff"`
	Branch    string      `json:"branch"`
	Mode      string      `json:"mode"`
}

// Server handles background lifecycle states and I/O routing for the Python daemon
type Server struct {
	Cmd       *exec.Cmd
	Stdin     io.WriteCloser
	Stdout    bufio.Reader
	StdoutRaw io.ReadCloser // raw pipe for early-exit reads before TUI starts
	Stderr    io.Reader
}

// NewServerDirect handles standalone compiled binaries
func NewServerDirect(binPath string, workspaceRoot ...string) (*Server, error) {
	cmd := exec.Command(binPath)
	env := append(os.Environ(), "KYREX_SURFACE=terminal")
	if len(workspaceRoot) > 0 && workspaceRoot[0] != "" {
		env = append(env, "WORKSPACE_ROOT="+workspaceRoot[0])
	}
	if len(workspaceRoot) > 1 && workspaceRoot[1] != "" {
		env = append(env, "PROJECT_SOURCE_ROOT="+workspaceRoot[1])
	}
	cmd.Env = env
	return startServer(cmd)
}

// NewServer spins up the Python engine script using the virtual env interpreter
func NewServer(pythonPath string, args []string, workspaceRoot ...string) (*Server, error) {
	cmd := exec.Command(pythonPath, args...)
	env := append(os.Environ(), "KYREX_SURFACE=terminal")
	if len(workspaceRoot) > 0 && workspaceRoot[0] != "" {
		env = append(env, "WORKSPACE_ROOT="+workspaceRoot[0])
	}
	if len(workspaceRoot) > 1 && workspaceRoot[1] != "" {
		env = append(env, "PROJECT_SOURCE_ROOT="+workspaceRoot[1])
	}
	cmd.Env = env
	return startServer(cmd)
}

func startServer(cmd *exec.Cmd) (*Server, error) {
	// If WORKSPACE_ROOT was added to env, set it as the subprocess CWD
	for _, e := range cmd.Env {
		if strings.HasPrefix(e, "WORKSPACE_ROOT=") {
			cmd.Dir = strings.TrimPrefix(e, "WORKSPACE_ROOT=")
			break
		}
	}
	stdinPipe, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}

	stdoutPipe, err := cmd.StdoutPipe()
	if err != nil {
		stdinPipe.Close()
		return nil, err
	}

	stderrPipe, err := cmd.StderrPipe()
	if err != nil {
		stdinPipe.Close()
		stdoutPipe.Close()
		return nil, err
	}

	if err := cmd.Start(); err != nil {
		stdinPipe.Close()
		stdoutPipe.Close()
		stderrPipe.Close()
		return nil, err
	}

	return &Server{
		Cmd:       cmd,
		Stdin:     stdinPipe,
		Stdout:    *bufio.NewReader(stdoutPipe),
		StdoutRaw: stdoutPipe,
		Stderr:    stderrPipe,
	}, nil
}

// Close handles graceful subprocess termination: SIGTERM → 3s grace → SIGKILL.
func (s *Server) Close() error {
	if s.Stdin != nil {
		s.Stdin.Close()
	}
	if s.Cmd == nil || s.Cmd.Process == nil {
		return nil
	}

	// Phase 1: Send SIGTERM and wait for graceful exit
	done := make(chan error, 1)
	go func() { done <- s.Cmd.Wait() }()

	if err := s.Cmd.Process.Signal(syscall.SIGTERM); err != nil {
		// Process already exited or signal not supported — try SIGKILL
		return s.Cmd.Process.Kill()
	}

	select {
	case <-done:
		return nil // Clean exit
	case <-time.After(3 * time.Second):
		// Phase 2: Grace period expired — force kill
		return s.Cmd.Process.Kill()
	}
}

// GetStderr routes background engine diagnostics directly to the log runner
func (s *Server) GetStderr() io.Reader {
	return s.Stderr
}

// Send accepts generic interface{} input to perfectly match tui.NewModel expectations
func (s *Server) Send(msg interface{}) error {
	var strMsg string
	switch v := msg.(type) {
	case string:
		strMsg = v
	default:
		bytes, err := json.Marshal(v)
		if err != nil {
			return err
		}
		strMsg = string(bytes)
	}
	_, err := io.WriteString(s.Stdin, strMsg+"\n")
	return err
}

// Next pulls, reads, and parses the sequential structural objects from the engine stream
func (s *Server) Next() (*Message, error) {
	line, err := s.Stdout.ReadString('\n')
	if err != nil {
		return nil, err
	}

	var msg Message
	if err := json.Unmarshal([]byte(line), &msg); err != nil {
		// Check if the line contains an error payload before falling back to log.
		// This prevents real errors (crashes, tracebacks) from being swallowed
		// as harmless "log" messages that the TUI ignores.
		if strings.Contains(line, `"type":"error"`) || strings.Contains(line, `"type": "error"`) {
			return &Message{Type: "error", Content: line}, nil
		}
		// Fallback for raw unformatted log text lines
		return &Message{Type: "log", Content: line}, nil
	}
	return &msg, nil
}
