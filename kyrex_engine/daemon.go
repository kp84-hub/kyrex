package kyrex_engine

// daemon.go — background engine daemon support for the TUI.
//
// The classic transport runs the engine as a child process wired over stdio:
// closing the TUI (or the terminal, or a crash) breaks the pipes and kills
// any in-flight turn. Daemon mode decouples the two lifecycles: the engine
// runs detached (KYREX_DAEMON=1), hosts a localhost TCP socket, buffers
// everything it emits for replay, auto-resolves approval gates while no UI
// is attached, and keeps working until an idle watchdog ends it. Quitting
// the TUI now just detaches — the session survives.
//
// The control-file key must match the Python side (daemon_bridge.py) and the
// IDE side (kyrex-ide/src-tauri/src/daemon.rs) exactly: FNV-1a 64 of the
// normalized workspace path, 16 lowercase hex chars.

import (
	"bufio"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

const (
	daemonConnectTimeout = 500 * time.Millisecond
	daemonStartTimeout   = 15 * time.Second
	daemonPollInterval   = 150 * time.Millisecond
)

// DaemonInfo mirrors the control file (~/.kyrex/daemons/<key>.json) written
// by daemon_bridge.py: {pid, port, workspace, project, started}.
type DaemonInfo struct {
	PID       int     `json:"pid"`
	Port      int     `json:"port"`
	Workspace string  `json:"workspace"`
	Project   string  `json:"project,omitempty"`
	Started   float64 `json:"started,omitempty"`
}

// normalizeDaemonWorkspace must mirror normalize_workspace in daemon_bridge.py
// and normalize_workspace in daemon.rs: separators to "/", trailing slashes
// trimmed, empty becomes "/".
func normalizeDaemonWorkspace(workspace string) string {
	p := strings.ReplaceAll(workspace, "\\", "/")
	p = strings.TrimRight(p, "/")
	if p == "" {
		return "/"
	}
	return p
}

// daemonFNV1a64 must mirror _fnv1a64 in daemon_bridge.py and fnv1a64 in
// daemon.rs. The "foobar" reference vector is covered by tests.
func daemonFNV1a64(data []byte) uint64 {
	const offset64 = uint64(0xcbf29ce484222325)
	const prime64 = uint64(0x100000001b3)
	h := offset64
	for _, b := range data {
		h ^= uint64(b)
		h *= prime64
	}
	return h
}

// DaemonKey returns the 16-hex-char control-file key for a workspace.
func DaemonKey(workspace string) string {
	return fmt.Sprintf("%016x", daemonFNV1a64([]byte(normalizeDaemonWorkspace(workspace))))
}

// daemonDir is where live daemons publish their {pid, port}. KYREX_HOME
// matches daemon_bridge.py's override; otherwise the user's home.
func daemonDir() string {
	if root := os.Getenv("KYREX_HOME"); root != "" {
		return filepath.Join(root, ".kyrex", "daemons")
	}
	home, err := os.UserHomeDir()
	if err != nil {
		home = os.Getenv("HOME")
	}
	return filepath.Join(home, ".kyrex", "daemons")
}

func daemonControlPath(workspace string) string {
	return filepath.Join(daemonDir(), DaemonKey(workspace)+".json")
}

// readDaemonInfo returns the recorded {pid, port} for this workspace, or nil.
func readDaemonInfo(workspace string) *DaemonInfo {
	raw, err := os.ReadFile(daemonControlPath(workspace))
	if err != nil {
		return nil
	}
	var info DaemonInfo
	if json.Unmarshal(raw, &info) != nil || info.Port <= 0 {
		return nil
	}
	return &info
}

// removeStaleDaemonControl prunes a control file whose port no longer
// answers, so later runs don't trust it.
func removeStaleDaemonControl(workspace string) {
	_ = os.Remove(daemonControlPath(workspace))
}

// listDaemonInfos parses every control file in the daemon dir. Corrupt or
// incomplete entries are skipped.
func listDaemonInfos() []*DaemonInfo {
	entries, err := os.ReadDir(daemonDir())
	if err != nil {
		return nil
	}
	var infos []*DaemonInfo
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".json") {
			continue
		}
		raw, err := os.ReadFile(filepath.Join(daemonDir(), e.Name()))
		if err != nil {
			continue
		}
		var info DaemonInfo
		if json.Unmarshal(raw, &info) != nil || info.Port <= 0 {
			continue
		}
		infos = append(infos, &info)
	}
	return infos
}

func dialDaemon(info *DaemonInfo) (net.Conn, error) {
	return net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", info.Port), daemonConnectTimeout)
}

// spawnDetachedDaemon starts the engine with KYREX_DAEMON=1 as a process
// that deliberately outlives the TUI: own process group on Unix,
// DETACHED_PROCESS on Windows, stdio routed to a log file instead of the
// (short-lived) app.
func spawnDetachedDaemon(engineBin, pythonPath string, scriptArgs []string, workspaceRoot, projectSource string) error {
	var cmd *exec.Cmd
	if engineBin != "" {
		cmd = exec.Command(engineBin)
	} else {
		cmd = exec.Command(pythonPath, scriptArgs...)
	}

	key := DaemonKey(workspaceRoot)
	cmd.Env = append(os.Environ(),
		"KYREX_SURFACE=terminal",
		"KYREX_DAEMON=1",
		"WORKSPACE_ROOT="+workspaceRoot,
		"PROJECT_SOURCE_ROOT="+projectSource,
	)
	cmd.Dir = workspaceRoot

	logPath := filepath.Join(os.TempDir(), "kyrex-daemon-"+key+".log")
	logFile, err := os.OpenFile(logPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return fmt.Errorf("daemon log: %w", err)
	}
	cmd.Stdin = nil
	cmd.Stdout = logFile
	cmd.Stderr = logFile

	configureDetached(cmd)

	if err := cmd.Start(); err != nil {
		logFile.Close()
		return fmt.Errorf("spawn daemon: %w", err)
	}
	// Reap whenever it eventually exits so it doesn't linger as a zombie.
	go func() {
		_ = cmd.Wait()
		logFile.Close()
	}()
	return nil
}

// AttachOrSpawnDaemon returns a Server wired to a background engine daemon
// for workspaceRoot, plus the directory the daemon is actually working in.
//
// Resolution order:
//  1. A daemon for exactly this workspace (same clone, or the IDE's daemon
//     on the project root itself).
//  2. A live daemon recorded for projectSource under a different clone root
//     — rift clones are recreated per run, so the workspace key changes
//     underneath a still-running daemon. Attaching to it lets the reopened
//     TUI pick up the live session instead of forking a second engine.
//  3. Spawn a fresh detached daemon and wait for its control file.
//
// The caller must adopt the returned workspace root when it differs from its
// own clone, so merge/sweep operate on the tree the daemon is editing. An
// error means daemon mode is unavailable — fall back to the classic child
// transport (which dies with the app).
func AttachOrSpawnDaemon(engineBin, pythonPath string, scriptArgs []string, workspaceRoot, projectSource string) (*Server, string, error) {
	// 1. Exact-workspace match.
	if info := readDaemonInfo(workspaceRoot); info != nil {
		if conn, err := dialDaemon(info); err == nil {
			return newDaemonServer(conn), normalizeDaemonWorkspace(info.Workspace), nil
		}
		removeStaleDaemonControl(workspaceRoot)
	}

	// 2. Project-level match (reattach across recreated clone roots).
	if projectSource != "" {
		if conn, info := findLiveProjectDaemon(projectSource); conn != nil {
			return newDaemonServer(conn), normalizeDaemonWorkspace(info.Workspace), nil
		}
	}

	// 3. Fresh detached daemon.
	if err := spawnDetachedDaemon(engineBin, pythonPath, scriptArgs, workspaceRoot, projectSource); err != nil {
		return nil, "", err
	}
	deadline := time.Now().Add(daemonStartTimeout)
	for time.Now().Before(deadline) {
		if info := readDaemonInfo(workspaceRoot); info != nil {
			if conn, err := dialDaemon(info); err == nil {
				return newDaemonServer(conn), normalizeDaemonWorkspace(info.Workspace), nil
			}
		}
		time.Sleep(daemonPollInterval)
	}
	return nil, "", fmt.Errorf(
		"engine daemon did not come up within %s (log: %s)",
		daemonStartTimeout,
		filepath.Join(os.TempDir(), "kyrex-daemon-"+DaemonKey(workspaceRoot)+".log"),
	)
}

// findLiveProjectDaemon scans the control files for a live daemon belonging
// to projectSource: newer control files record the project explicitly; older
// ones are matched by the workspace path living in that project's rift
// storage. Only daemons whose port actually answers are returned.
func findLiveProjectDaemon(projectSource string) (net.Conn, *DaemonInfo) {
	normalized := normalizeDaemonWorkspace(projectSource)
	clonesDir := ""
	if base := filepath.Base(projectSource); base != "" && base != "." && base != "/" {
		clonesDir = normalizeDaemonWorkspace(
			filepath.Join(filepath.Dir(projectSource), ".rifts", base))
	}
	for _, info := range listDaemonInfos() {
		if info.Project != "" {
			if normalizeDaemonWorkspace(info.Project) != normalized {
				continue
			}
		} else if clonesDir == "" ||
			!strings.HasPrefix(normalizeDaemonWorkspace(info.Workspace), clonesDir+"/") {
			continue
		}
		conn, err := dialDaemon(info)
		if err != nil {
			continue
		}
		return conn, info
	}
	return nil, nil
}

// newDaemonServer wraps a TCP connection to a live daemon in the same
// Server transport the TUI already speaks (newline-delimited JSON).
func newDaemonServer(conn net.Conn) *Server {
	return &Server{
		Stdin:  conn,
		Stdout: *bufio.NewReader(conn),
		conn:   conn,
		daemon: true,
	}
}
