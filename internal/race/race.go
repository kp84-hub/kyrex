// Package race implements parallel "race mode" — running the same coding task
// across N models in parallel, each in an isolated clone of the workspace.
// It is headless: it does not import bubbletea and communicates results via
// callback functions, making it compatible with both TUI embedding and
// CLI-driven race orchestration.
package race

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"time"
)

// CloneExcludes lists directory patterns excluded from workspace clones via
// rsync --exclude flags. These are directories whose contents would waste
// clone time and storage (large generated dirs, caches, version control).
var CloneExcludes = []string{
	".venv", "venv", "build_venv", "node_modules",
	"__pycache__", ".rifts", "dist", "build", "target",
}

// ── LaneStatus ────────────────────────────────────────────────────────────

// LaneStatus represents the lifecycle state of a single race lane.
type LaneStatus int

const (
	LanePending LaneStatus = iota
	LaneRunning
	LaneDone
	LaneFailed
	LaneKilled
)

func (s LaneStatus) String() string {
	switch s {
	case LanePending:
		return "pending"
	case LaneRunning:
		return "running"
	case LaneDone:
		return "done"
	case LaneFailed:
		return "failed"
	case LaneKilled:
		return "killed"
	default:
		return "unknown"
	}
}

// ── Event (engine NDJSON message) ─────────────────────────────────────────

// Event represents a single NDJSON line emitted by the Python engine on stdout.
// Fields use json tags matching the established IPC protocol.
type Event struct {
	Type    string `json:"type"`
	Content string `json:"content"`
	Message string `json:"message"`
	Name    string `json:"name"`
	ID      string `json:"id"`
	Path    string `json:"path"`
	Value   string `json:"value"`
	Diff    string `json:"diff"`
	Result  any    `json:"result"`
	Args    any    `json:"args"`
}

// IsTool returns true when the event signals a tool call start.
func (e Event) IsTool() bool { return e.Type == "tool_start" }

// IsDone returns true when the event signals turn completion.
func (e Event) IsDone() bool { return e.Type == "chat_done" }

// IsError returns true when the event signals an error.
func (e Event) IsError() bool { return e.Type == "error" }

// ErrText returns the error text from either Content or Message, whichever is set.
func (e Event) ErrText() string {
	if e.Message != "" {
		return e.Message
	}
	return e.Content
}

// IsText returns true when the event is a streaming text token.
func (e Event) IsText() bool { return e.Type == "token" }

// ── LaneMsg / LaneExitMsg ─────────────────────────────────────────────────

// LaneMsg is forwarded to the send callback each time a lane emits an Event.
type LaneMsg struct {
	LaneID int
	Event  Event
}

// LaneExitMsg is forwarded when a lane's stdout stream terminates.
// Err is the result of cmd.Wait().
type LaneExitMsg struct {
	LaneID int
	Err    error
}

// ── Lane ──────────────────────────────────────────────────────────────────

// Lane represents a single model run in an isolated clone of the workspace.
// It owns the subprocess and handles all IPC with the engine.
type Lane struct {
	ID         int
	Model      string
	Dir        string // clone directory
	Status     LaneStatus
	Rounds     int
	LastTool   string
	Err        string
	StartedAt  time.Time
	FinishedAt time.Time

	cmd   *exec.Cmd
	stdin io.WriteCloser
	mu    sync.Mutex
	tail  []string
}

// AppendTail appends a line to the tail buffer, keeping at most the last 3.
func (l *Lane) AppendTail(line string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.tail = append(l.tail, line)
	if len(l.tail) > 3 {
		l.tail = l.tail[len(l.tail)-3:]
	}
}

// Tail returns a copy of the last 3 streamed lines.
func (l *Lane) Tail() []string {
	l.mu.Lock()
	defer l.mu.Unlock()
	out := make([]string, len(l.tail))
	copy(out, l.tail)
	return out
}

// SendLine marshals data as JSON, appends a newline, and writes it to the
// engine's stdin. Mutex-guarded because the reader goroutine also writes
// auto-approve confirm_responses to stdin.
func (l *Lane) SendLine(data map[string]any) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.stdin == nil {
		return fmt.Errorf("lane %d: stdin not available", l.ID)
	}
	b, err := json.Marshal(data)
	if err != nil {
		return err
	}
	b = append(b, '\n')
	_, err = l.stdin.Write(b)
	return err
}

// Kill terminates the lane's subprocess if running and sets status to killed.
func (l *Lane) Kill() {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.cmd != nil && l.cmd.Process != nil {
		_ = l.cmd.Process.Kill()
	}
	if l.Status == LaneRunning {
		l.Status = LaneKilled
	}
	l.FinishedAt = time.Now()
}

// Spawn prepares the engine subprocess command without starting it.
// engineCmd is the command and args (e.g. ["python3", "core_bridge.py"]).
// logDir is where lane-<id>.stderr.log is created.
func (l *Lane) Spawn(engineCmd []string, logDir string) error {
	if len(engineCmd) == 0 {
		return fmt.Errorf("lane %d: engine command is empty", l.ID)
	}

	cmd := exec.Command(engineCmd[0], engineCmd[1:]...)
	cmd.Dir = l.Dir

	// Environment: inherit current process env, then override workspace roots.
	env := os.Environ()
	env = append(env, "WORKSPACE_ROOT="+l.Dir)
	env = append(env, "PROJECT_SOURCE_ROOT="+l.Dir)
	cmd.Env = env

	// Stderr -> log file
	if err := os.MkdirAll(logDir, 0755); err != nil {
		return fmt.Errorf("lane %d: mkdir log dir: %w", l.ID, err)
	}
	logPath := filepath.Join(logDir, fmt.Sprintf("lane-%d.stderr.log", l.ID))
	f, err := os.Create(logPath)
	if err != nil {
		return fmt.Errorf("lane %d: create stderr log: %w", l.ID, err)
	}
	cmd.Stderr = f

	l.cmd = cmd
	return nil
}

// StartReader wires stdout/stdin pipes, calls cmd.Start(), and launches a
// goroutine that scans stdout for NDJSON events. For every "confirm_request"
// event it IMMEDIATELY replies with an auto-approve confirm_response so that
// headless lanes never block on diff/deletion gates inside disposable clones.
// Events are forwarded to send(LaneMsg{...}); when stdout closes,
// send(LaneExitMsg{...}) is called with the cmd.Wait() error.
func (l *Lane) StartReader(send func(msg any)) error {
	l.mu.Lock()
	cmd := l.cmd
	l.mu.Unlock()

	if cmd == nil {
		return fmt.Errorf("lane %d: Spawn must be called before StartReader", l.ID)
	}

	stdin, err := cmd.StdinPipe()
	if err != nil {
		return fmt.Errorf("lane %d: stdin pipe: %w", l.ID, err)
	}

	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return fmt.Errorf("lane %d: stdout pipe: %w", l.ID, err)
	}

	if err := cmd.Start(); err != nil {
		return fmt.Errorf("lane %d: start: %w", l.ID, err)
	}

	l.mu.Lock()
	l.stdin = stdin
	l.Status = LaneRunning
	l.StartedAt = time.Now()
	l.mu.Unlock()

	go l.readLoop(stdout, stdin, send, cmd)
	return nil
}

// readLoop is the goroutine that reads NDJSON events from the engine's stdout.
func (l *Lane) readLoop(stdout io.ReadCloser, stdin io.WriteCloser, send func(msg any), cmd *exec.Cmd) {
	defer stdout.Close()

	scanner := bufio.NewScanner(stdout)
	// 1 MiB buffer — diff payloads can be large
	buf := make([]byte, 1024*1024)
	scanner.Buffer(buf, len(buf))

	for scanner.Scan() {
		line := scanner.Text()
		if line == "" {
			continue
		}

		l.AppendTail(line)

		var ev Event
		if err := json.Unmarshal([]byte(line), &ev); err != nil {
			// Malformed line — skip, never fatal
			continue
		}

		// Auto-approve confirmation gates (diff edits, deletions) in
		// the disposable clone so the engine never blocks.
		if ev.Type == "confirm_request" {
			_ = l.SendLine(map[string]any{
				"type":     "confirm_response",
				"id":       ev.ID,
				"approved": true,
			})
		}

		send(LaneMsg{LaneID: l.ID, Event: ev})
	}

	waitErr := cmd.Wait()

	l.mu.Lock()
	l.Status = LaneDone
	l.FinishedAt = time.Now()
	l.mu.Unlock()

	send(LaneExitMsg{LaneID: l.ID, Err: waitErr})
}

// ── Race ──────────────────────────────────────────────────────────────────

// Race orchestrates parallel model runs. It owns the lane clones and tracks
// aggregate timing.
type Race struct {
	Task      string
	SrcDir    string
	Dir       string      // parent directory containing all lane-<i> clones
	Lanes     []*Lane     // one per model, populated after New
	StartedAt time.Time
	RoundCap  int         // max rounds per lane, default 25
	CloneSecs []float64   // per-lane clone duration in seconds
}

// New creates a Race by cloning srcDir once per model in parallel into
// baseDir/lane-<i>. Each clone gets a .kx-lane marker and its model set
// in .px/config.json.
//
// Clones are performed concurrently using a sync.WaitGroup pattern for
// isolation and error collection.
func New(task, srcDir, baseDir string, models []string) (*Race, error) {
	r := &Race{
		Task:      task,
		SrcDir:    srcDir,
		Dir:       baseDir,
		Lanes:     make([]*Lane, len(models)),
		StartedAt: time.Now(),
		RoundCap:  25,
		CloneSecs: make([]float64, len(models)),
	}

	if srcDir == "" {
		return nil, fmt.Errorf("race: srcDir is empty")
	}
	if len(models) == 0 {
		return nil, fmt.Errorf("race: no models provided")
	}

	var (
		wg     sync.WaitGroup
		mu     sync.Mutex
		first  error
	)

	for i, model := range models {
		i, model := i, model
		wg.Add(1)
		go func() {
			defer wg.Done()
			err := r.cloneLane(i, model, srcDir, baseDir)
			if err != nil {
				mu.Lock()
				if first == nil {
					first = err
				}
				mu.Unlock()
			}
		}()
	}
	wg.Wait()

	// If any clone failed, return only the first error and clean up.
	if first != nil {
		for _, ln := range r.Lanes {
			if ln != nil {
				_ = os.RemoveAll(ln.Dir)
			}
		}
		return nil, first
	}

	return r, nil
}

// cloneLane performs all clone setup for one lane index (i).
func (r *Race) cloneLane(i int, model, srcDir, baseDir string) error {
	cloneStart := time.Now()
	laneDir := filepath.Join(baseDir, fmt.Sprintf("lane-%d", i))

	// Remove stale clone if it exists
	if fi, err := os.Stat(laneDir); err == nil && fi.IsDir() {
		if err := os.RemoveAll(laneDir); err != nil {
			return fmt.Errorf("lane %d: remove stale clone: %w", i, err)
		}
	}

	// Clone via rsync (preferred) or cp fallback
	if err := os.MkdirAll(laneDir, 0o755); err != nil {
		return fmt.Errorf("lane %d: mkdir: %w", i, err)
	}
	if err := cloneDir(srcDir, laneDir); err != nil {
		return fmt.Errorf("lane %d: clone failed: %w", i, err)
	}

	// Write .kx-lane marker
	marker := fmt.Sprintf("lane=%d\nmodel=%s\nrace=%s\n", i, model, baseDir)
	if err := os.WriteFile(filepath.Join(laneDir, ".kx-lane"), []byte(marker), 0644); err != nil {
		return fmt.Errorf("lane %d: write .kx-lane: %w", i, err)
	}

	// Set model in .px/config.json
	if err := setModelInConfig(laneDir, model); err != nil {
		return fmt.Errorf("lane %d: set model config: %w", i, err)
	}

	// Record clone duration
	r.CloneSecs[i] = time.Since(cloneStart).Seconds()

	// Create Lane
	r.Lanes[i] = &Lane{
		ID:     i,
		Model:  model,
		Dir:    laneDir,
		Status: LanePending,
	}
	return nil
}

// clone helpers -------------------------------------------------------------

// cloneDir copies the contents of src into dst using rsync -a (preferred) or
// cp -a --reflink=auto as a fallback. Trailing slashes are used so that the
// contents (not the src directory itself) are copied into dst.
func cloneDir(src, dst string) error {
	if _, err := exec.LookPath("rsync"); err == nil {
		args := []string{"-a"}
		for _, excl := range CloneExcludes {
			args = append(args, "--exclude", excl)
		}
		args = append(args, src+"/", dst+"/")
		cmd := exec.Command("rsync", args...)
		if out, err := cmd.CombinedOutput(); err != nil {
			return fmt.Errorf("rsync failed: %w\n%s", err, string(out))
		}
		return nil
	}

	// Fallback: cp -a --reflink=auto (excludes not supported by cp)
	_, _ = fmt.Fprintf(os.Stderr,
		"WARNING: rsync not found, using cp -a --reflink=auto (excludes not applied)\n")
	if err := os.MkdirAll(dst, 0755); err != nil {
		return fmt.Errorf("cp: mkdir dst: %w", err)
	}
	cmd := exec.Command("cp", "-a", "--reflink=auto", src+"/.", dst+"/")
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("cp failed: %w\n%s", err, string(out))
	}
	return nil
}

// setModelInConfig ensures <laneDir>/.px/config.json has the "model" field
// set to the given model name.
//
// The engine reads this key as "model" — confirmed in:
//
//	kyrex_engine/kyrex/core.py:88      — self._config.get("model")
//	kyrex_engine/kyrex/config.py:72    — self._data = {k.lower(): v for k, v in cfg.items()}
//	kyrex_engine/core_bridge.py:290    — cfg = ConfigManager(Path(WORKSPACE_ROOT) / ".px" / "config.json")
func setModelInConfig(laneDir, model string) error {
	pxDir := filepath.Join(laneDir, ".px")
	configPath := filepath.Join(pxDir, "config.json")

	// Check if .px dir exists
	pxInfo, pxErr := os.Stat(pxDir)
	pxExists := pxErr == nil && pxInfo.IsDir()

	if pxExists {
		// .px exists — check for config.json
		data, readErr := os.ReadFile(configPath)
		if readErr == nil {
			// Parse existing JSON
			var cfg map[string]any
			if err := json.Unmarshal(data, &cfg); err != nil {
				return fmt.Errorf(".px/config.json exists but is unparseable: %w", err)
			}
			cfg["model"] = model
			return writeJSON(configPath, cfg)
		}
		if !os.IsNotExist(readErr) {
			return fmt.Errorf("read config.json: %w", readErr)
		}
		// .px exists but config.json doesn't — create it below
	} else {
		// .px doesn't exist — create it
		if err := os.MkdirAll(pxDir, 0755); err != nil {
			return fmt.Errorf("mkdir .px: %w", err)
		}
	}

	// Write fresh config with model field
	cfg := map[string]any{"model": model}
	return writeJSON(configPath, cfg)
}

func writeJSON(path string, v any) error {
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal config: %w", err)
	}
	b = append(b, '\n')
	if err := os.WriteFile(path, b, 0644); err != nil {
		return fmt.Errorf("write config: %w", err)
	}
	return nil
}

// VerifyLane reads the .kx-lane marker in the lane's clone directory and
// errors if the lane ID does not match. Callers must invoke this before
// diffing or merging to guarantee they are operating on the correct clone.
func VerifyLane(l *Lane) error {
	data, err := os.ReadFile(filepath.Join(l.Dir, ".kx-lane"))
	if err != nil {
		return fmt.Errorf("lane %d: cannot read .kx-lane: %w", l.ID, err)
	}
	idStr := fmt.Sprintf("lane=%d\n", l.ID)
	if len(data) < len(idStr) || string(data[:len(idStr)]) != idStr {
		return fmt.Errorf("lane %d: .kx-lane ID mismatch", l.ID)
	}
	return nil
}

// ── Lifecycle ──────────────────────────────────────────────────────────────

// AllSettled returns true when no lane is pending or running. Failed and
// killed lanes count as settled (one lane failing must not abort a race).
// A nil lane pointer is treated as not settled.
func (r *Race) AllSettled() bool {
	for _, l := range r.Lanes {
		if l == nil {
			return false
		}
		switch l.Status {
		case LanePending, LaneRunning:
			return false
		}
	}
	return true
}

// DiffLane runs diff -ruN between r.SrcDir and l.Dir, excluding the same
// patterns as CloneExcludes plus .kx-lane and .px metadata. Exit code 1
// means files differ — that is success, return the diff output with nil
// error. Exit code 0 means identical trees. Any other exit code is a real
// error propagated via the returned error value.
func (r *Race) DiffLane(l *Lane) (string, error) {
	args := []string{"-ruN"}
	for _, excl := range CloneExcludes {
		args = append(args, "-x", excl)
	}
	args = append(args, "-x", ".kx-lane", "-x", ".px*", "-x", ".git")
	args = append(args, r.SrcDir, l.Dir)

	cmd := exec.Command("diff", args...)
	out, err := cmd.CombinedOutput()
	if err == nil {
		// Exit 0 — identical trees
		return "", nil
	}
	if exitErr, ok := err.(*exec.ExitError); ok {
		if exitErr.ExitCode() == 1 {
			// Exit 1 — files differ, output is the diff
			return string(out), nil
		}
	}
	// Any other exit code or non-exit error is a real error
	exitCode := -1
	if cmd.ProcessState != nil {
		exitCode = cmd.ProcessState.ExitCode()
	}
	return "", fmt.Errorf("diff failed (exit %d): %w\n%s", exitCode, err, string(out))
}

// DefaultGateCommand returns a sensible default shell command to verify that
// a lane's workspace is self-consistent. When <dir>/go.mod exists it returns
// "go build ./..."; otherwise it returns "true" (a no-op pass). In future
// versions this result may be overridden by configuration (e.g. a per-project
// .kx-gate file or the race config).
func DefaultGateCommand(dir string) string {
	if _, err := os.Stat(filepath.Join(dir, "go.mod")); err == nil {
		return "go build ./..."
	}
	return "true"
}

// GateLane runs command via sh -c inside the lane's working directory with
// environment inherited from os.Environ(). Combined output is captured; if the
// process does not exit within timeout it is killed. The output is truncated to
// the last 4 KB (tail of failure is the most useful part). passed = (exit code
// == 0). If the lane is nil or its directory is empty the method returns an
// error immediately without running anything.
func (r *Race) GateLane(l *Lane, command string, timeout time.Duration) (passed bool, output string, err error) {
	if l == nil {
		return false, "", fmt.Errorf("gate: lane is nil")
	}
	if l.Dir == "" {
		return false, "", fmt.Errorf("gate: lane %d has no directory", l.ID)
	}
	if command == "" {
		return false, "", fmt.Errorf("gate: command is empty")
	}
	if timeout <= 0 {
		timeout = 120 * time.Second
	}

	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()

	cmd := exec.CommandContext(ctx, "sh", "-c", command)
	cmd.Dir = l.Dir
	cmd.Env = os.Environ()

	out, runErr := cmd.CombinedOutput()
	output = string(out)

	// Truncate to last 4 KB (failure tail)
	if len(output) > 4096 {
		output = output[len(output)-4096:]
	}

	if ctx.Err() == context.DeadlineExceeded {
		return false, output, fmt.Errorf("gate: lane %d command timed out after %v", l.ID, timeout)
	}

	if runErr == nil {
		return true, output, nil
	}

	// Non-zero exit is a failure, not a gate error
	if exitErr, ok := runErr.(*exec.ExitError); ok {
		return exitErr.ExitCode() == 0, output, nil
	}

	return false, output, runErr
}

// KillAll kills every non-nil lane, regardless of current status.
func (r *Race) KillAll() {
	for _, l := range r.Lanes {
		if l != nil {
			l.Kill()
		}
	}
}

// Cleanup kills all lanes, waits briefly for kills to land, then removes
// the entire race directory tree (r.Dir).
func (r *Race) Cleanup() error {
	r.KillAll()
	time.Sleep(200 * time.Millisecond)
	return os.RemoveAll(r.Dir)
}
