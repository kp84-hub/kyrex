package rift

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// gitInitRepo initializes dir as a git repo with everything currently on disk
// committed, so subsequent modifications show up as working-tree changes.
func gitInitRepo(t *testing.T, dir string) {
	t.Helper()
	steps := [][]string{
		{"init", "-q"},
		{"add", "-A"},
		{"-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "init"},
	}
	for _, args := range steps {
		cmd := exec.Command("git", append([]string{"-C", dir}, args...)...)
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("git %v: %v: %s", args, err, out)
		}
	}
}

// TestRevertFileRestoresModifiedTracked: a command-modified tracked file is
// restored from HEAD.
func TestRevertFileRestoresModifiedTracked(t *testing.T) {
	root := t.TempDir()
	target := filepath.Join(root, "tracked.txt")
	if err := os.WriteFile(target, []byte("base\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	gitInitRepo(t, root)

	// The command rewrote the tracked file.
	if err := os.WriteFile(target, []byte("modified by command\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	m := New()
	if err := m.RevertFile(&Workspace{Root: root, Source: t.TempDir()}, target); err != nil {
		t.Fatalf("RevertFile(modified tracked) = %v; want nil", err)
	}
	data, err := os.ReadFile(target)
	if err != nil {
		t.Fatal(err)
	}
	if string(data) != "base\n" {
		t.Fatalf("tracked file not restored to HEAD: %q", data)
	}
	// The clone must be clean afterwards.
	if status, _ := gitStatusFor(root, "tracked.txt"); status != "" {
		t.Fatalf("clone still dirty after revert: %q", status)
	}
}

// TestRevertFileRemovesUntracked: a file the command newly created (untracked,
// including staged-for-the-first-time) is removed, not restored.
func TestRevertFileRemovesUntracked(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "keep.txt"), []byte("k\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	gitInitRepo(t, root)

	created := filepath.Join(root, "created.txt")
	if err := os.WriteFile(created, []byte("c\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	m := New()
	if err := m.RevertFile(&Workspace{Root: root, Source: t.TempDir()}, created); err != nil {
		t.Fatalf("RevertFile(untracked) = %v; want nil", err)
	}
	if _, err := os.Stat(created); !os.IsNotExist(err) {
		t.Fatalf("untracked leftover not removed: %v", err)
	}
	// A tracked file the command did not touch is untouched.
	if _, err := os.Stat(filepath.Join(root, "keep.txt")); err != nil {
		t.Fatalf("unrelated tracked file disturbed: %v", err)
	}
}

// TestRevertFileContainment: paths outside the workspace, the workspace root
// itself, and merge-ignored artifacts are all rejected.
func TestRevertFileContainment(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "in.txt"), []byte("i\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	gitInitRepo(t, root)
	ws := &Workspace{Root: root, Source: t.TempDir()}
	m := New()

	outside := filepath.Join(t.TempDir(), "outside.txt")
	if err := os.WriteFile(outside, []byte("x\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := m.RevertFile(ws, outside); err == nil || !strings.Contains(err.Error(), "not inside workspace") {
		t.Fatalf("outside path: got %v; want containment rejection", err)
	}
	if err := m.RevertFile(ws, root); err == nil || !strings.Contains(err.Error(), "not inside workspace") {
		t.Fatalf("workspace root: got %v; want containment rejection", err)
	}
	if err := m.RevertFile(ws, filepath.Join(root, ".px_history")); err == nil {
		t.Fatal("merge-ignored artifact must be rejected")
	}
}

// TestRevertFileLeavesPreExistingDirtyUntouched: RevertFile only ever touches
// the exact path it is handed. A pre-existing dirty file (the operator's own
// uncommitted work, excluded upstream by the session baseline) is never
// reverted.
func TestRevertFileLeavesPreExistingDirtyUntouched(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "base.txt"), []byte("base\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	gitInitRepo(t, root)

	dirty := filepath.Join(root, "preexisting.txt")
	if err := os.WriteFile(dirty, []byte("operator dirt\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	cmd := filepath.Join(root, "cmd.txt")
	if err := os.WriteFile(cmd, []byte("cmd\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	m := New()
	if err := m.RevertFile(&Workspace{Root: root, Source: t.TempDir()}, cmd); err != nil {
		t.Fatalf("RevertFile(cmd) = %v; want nil", err)
	}
	if _, err := os.Stat(cmd); !os.IsNotExist(err) {
		t.Fatalf("command-introduced file not reverted: %v", err)
	}
	data, err := os.ReadFile(dirty)
	if err != nil || string(data) != "operator dirt\n" {
		t.Fatalf("pre-existing dirty file was touched: err=%v content=%q", err, data)
	}
}
