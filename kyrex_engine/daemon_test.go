package kyrex_engine

// daemon_test.go — coverage for the background-daemon transport: key parity
// with daemon_bridge.py / daemon.rs, control-file discovery, project-level
// reattach across recreated rift clone roots, and detach-without-kill close
// semantics.

import (
	"bufio"
	"encoding/json"
	"io"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestDaemonKeyMatchesPythonAndRust(t *testing.T) {
	// FNV-1a 64 reference vector ("foobar") — the same constant the Python
	// and Rust sides use, so all three must agree byte for byte.
	if got := daemonFNV1a64([]byte("foobar")); got != 0x85944171f73967e8 {
		t.Fatalf("fnv1a64(foobar) = %#x, want 0x85944171f73967e8", got)
	}
	// Normalization parity: trailing separators collapse, backslashes
	// normalize, distinct workspaces stay distinct.
	if DaemonKey("/a/b") != DaemonKey("/a/b/") {
		t.Fatal("trailing slash must not change the key")
	}
	if DaemonKey(`C:\repo\sub`) != DaemonKey("C:/repo/sub") {
		t.Fatal("separator normalization mismatch")
	}
	if DaemonKey("/ws/a") == DaemonKey("/ws/b") {
		t.Fatal("distinct workspaces must not share a key")
	}
}

// startFakeDaemon accepts one connection, sends a session_replay marker,
// captures the first line the client sends, then parks the connection open —
// a real daemon does not die when a client leaves.
func startFakeDaemon(t *testing.T, marker string) (addr string, firstLine <-chan string) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	lines := make(chan string, 1)
	go func() {
		conn, err := ln.Accept()
		if err != nil {
			return
		}
		if marker != "" {
			conn.Write([]byte(marker + "\n"))
		}
		r := bufio.NewReader(conn)
		if line, err := r.ReadString('\n'); err == nil {
			lines <- line
		} else {
			lines <- ""
		}
		io.Copy(io.Discard, conn)
	}()
	return ln.Addr().String(), lines
}

func mustPort(t *testing.T, addr string) int {
	t.Helper()
	_, portStr, err := net.SplitHostPort(addr)
	if err != nil {
		t.Fatal(err)
	}
	port, err := strconv.Atoi(portStr)
	if err != nil {
		t.Fatal(err)
	}
	return port
}

func writeTestControlFile(t *testing.T, workspace string, port int, project string) {
	t.Helper()
	path := daemonControlPath(workspace)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	info := DaemonInfo{
		PID:       os.Getpid(),
		Port:      port,
		Workspace: normalizeDaemonWorkspace(workspace),
		Project:   project,
	}
	raw, err := json.Marshal(info)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, raw, 0o644); err != nil {
		t.Fatal(err)
	}
}

// closedPort returns a port that is guaranteed to refuse connections.
func closedPort(t *testing.T) int {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := ln.Addr().(*net.TCPAddr).Port
	ln.Close()
	return port
}

func TestAttachOrSpawnDaemonAttachesToLiveDaemon(t *testing.T) {
	t.Setenv("KYREX_HOME", t.TempDir())
	addr, firstLine := startFakeDaemon(t,
		`{"type":"session_replay","count":2,"branch":"main","pid":1}`)
	ws := t.TempDir()
	writeTestControlFile(t, ws, mustPort(t, addr), "")

	srv, root, err := AttachOrSpawnDaemon("", "kyrex-engine-nonexistent", nil, ws, "")
	if err != nil {
		t.Fatalf("attach: %v", err)
	}
	defer srv.Close()
	if !srv.IsDaemon() {
		t.Fatal("expected the daemon transport, got the child transport")
	}
	if DaemonKey(root) != DaemonKey(ws) {
		t.Fatalf("adopted root %q does not match workspace %q", root, ws)
	}

	if err := srv.Send(map[string]interface{}{"type": "chat", "content": "hi"}); err != nil {
		t.Fatalf("send: %v", err)
	}
	select {
	case line := <-firstLine:
		if !strings.Contains(line, `"chat"`) {
			t.Fatalf("daemon received unexpected line: %q", line)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("daemon never received the sent line")
	}

	msg, err := srv.Next()
	if err != nil {
		t.Fatalf("next: %v", err)
	}
	if msg.Type != "session_replay" || msg.Count != 2 || msg.Branch != "main" {
		t.Fatalf("expected the session_replay marker, got %+v", msg)
	}

	// Close detaches without killing the daemon: the port still answers and
	// the control file survives so a later run reattaches.
	if err := srv.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	c2, err := net.DialTimeout("tcp", addr, time.Second)
	if err != nil {
		t.Fatalf("daemon must survive Close: %v", err)
	}
	c2.Close()
	if readDaemonInfo(ws) == nil {
		t.Fatal("control file must survive detach")
	}
}

func TestAttachOrSpawnDaemonFindsProjectDaemonUnderNewClone(t *testing.T) {
	t.Setenv("KYREX_HOME", t.TempDir())
	project := t.TempDir()
	addr, _ := startFakeDaemon(t, `{"type":"session_replay","count":0}`)

	// A clone root from the previous run — the directory may even be gone;
	// only the recorded project links it to this run.
	oldClone := filepath.Join(filepath.Dir(project), ".rifts", filepath.Base(project), "old-clone")
	writeTestControlFile(t, oldClone, mustPort(t, addr), project)

	srv, root, err := AttachOrSpawnDaemon("", "kyrex-engine-nonexistent", nil, t.TempDir(), project)
	if err != nil {
		t.Fatalf("reattach across clone roots: %v", err)
	}
	defer srv.Close()
	if DaemonKey(root) != DaemonKey(oldClone) {
		t.Fatalf("adopted %q, want the daemon's own clone %q", root, oldClone)
	}
}

func TestAttachOrSpawnDaemonPrunesStaleControlFile(t *testing.T) {
	t.Setenv("KYREX_HOME", t.TempDir())
	ws := t.TempDir()
	writeTestControlFile(t, ws, closedPort(t), "")
	path := daemonControlPath(ws)

	_, _, err := AttachOrSpawnDaemon("", "kyrex-engine-nonexistent", nil, ws, "")
	if err == nil {
		t.Fatal("expected failure to spawn a nonexistent engine")
	}
	if _, statErr := os.Stat(path); !os.IsNotExist(statErr) {
		t.Fatal("stale control file must be pruned after a failed dial")
	}
}

func TestFindLiveProjectDaemonMatchesByClonePath(t *testing.T) {
	t.Setenv("KYREX_HOME", t.TempDir())
	project := t.TempDir()
	addr, _ := startFakeDaemon(t, "")

	// No project field recorded — older control files are matched by the
	// workspace path living inside the project's rift storage.
	oldClone := filepath.Join(filepath.Dir(project), ".rifts", filepath.Base(project), "old-clone")
	writeTestControlFile(t, oldClone, mustPort(t, addr), "")

	conn, info := findLiveProjectDaemon(project)
	if conn == nil {
		t.Fatal("expected to find the daemon by clone-path prefix")
	}
	conn.Close()
	if DaemonKey(info.Workspace) != DaemonKey(oldClone) {
		t.Fatalf("got workspace %q, want %q", info.Workspace, oldClone)
	}

	// Daemons must not leak across projects.
	if other, _ := findLiveProjectDaemon(t.TempDir()); other != nil {
		other.Close()
		t.Fatal("a project's daemon was returned for an unrelated project")
	}
}
