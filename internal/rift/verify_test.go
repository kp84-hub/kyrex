package rift

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// A destination that drifts from the clone must be reported, not silently
// accepted. That is the case that matters: the TUI merges on a fixed sleep,
// so copyFile can run against pre-write content and still report success.
func TestVerifyCopyDetectsMismatch(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "src.txt")
	dst := filepath.Join(dir, "dst.txt")

	if err := os.WriteFile(src, []byte("new content\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(dst, []byte("new content\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := verifyCopy(src, dst); err != nil {
		t.Fatalf("identical files should verify, got: %v", err)
	}

	if err := os.WriteFile(dst, []byte("stale content\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	err := verifyCopy(src, dst)
	if err == nil {
		t.Fatal("mismatch verified clean - a stale merge would report success")
	}
	if !strings.Contains(err.Error(), "NOT been applied") {
		t.Fatalf("error must say the change did not land, got: %v", err)
	}
}

func TestMergeFileVerifies(t *testing.T) {
	root := t.TempDir()
	source := t.TempDir()
	ws := &Workspace{Root: root, Source: source}

	clonePath := filepath.Join(root, "a.txt")
	if err := os.WriteFile(clonePath, []byte("approved change\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	m := New()
	if err := m.MergeFile(ws, clonePath); err != nil {
		t.Fatalf("MergeFile should succeed on a clean copy: %v", err)
	}
	got, err := os.ReadFile(filepath.Join(source, "a.txt"))
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "approved change\n" {
		t.Fatalf("merged content wrong: %q", got)
	}
}
