package kyrex_engine

import (
	"bufio"
	"encoding/json"
	"io"
	"os/exec"
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
	Files     []string    `json:"files"`
	Stdout    string      `json:"stdout"`
	Reasoning string      `json:"reasoning"`
	Todos     []string    `json:"todos"`
	Path      string      `json:"path"`
	Diff      string      `json:"diff"`
	Branch    string      `json:"branch"`
	Mode      string      `json:"mode"`
}

// Server handles background lifecycle states and I/O routing for the Python daemon
type Server struct {
	Cmd    *exec.Cmd
	Stdin  io.WriteCloser
	Stdout bufio.Reader
	Stderr io.Reader
}

// NewServerDirect handles standalone compiled binaries
func NewServerDirect(binPath string) (*Server, error) {
	cmd := exec.Command(binPath)
	return startServer(cmd)
}

// NewServer spins up the Python engine script using the virtual env interpreter
func NewServer(pythonPath string, args ...string) (*Server, error) {
	cmd := exec.Command(pythonPath, args...)
	return startServer(cmd)
}

func startServer(cmd *exec.Cmd) (*Server, error) {
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
		Cmd:    cmd,
		Stdin:  stdinPipe,
		Stdout: *bufio.NewReader(stdoutPipe),
		Stderr: stderrPipe,
	}, nil
}

// Close handles smooth subprocess termination and pipe teardown
func (s *Server) Close() error {
	if s.Stdin != nil {
		s.Stdin.Close()
	}
	if s.Cmd != nil && s.Cmd.Process != nil {
		return s.Cmd.Process.Kill()
	}
	return nil
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
		// Fallback to avoid breaking on raw unformatted log text lines
		return &Message{Type: "log", Content: line}, nil
	}
	return &msg, nil
}
