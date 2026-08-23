package rift

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// A Bot workspace is its durable state. If Discard removes one, the Bot loses
// everything it has, and it loses it silently.
func TestDiscardRefusesPersistent(t *testing.T) {
	dir := t.TempDir()
	root := filepath.Join(dir, "clone")
	if err := os.MkdirAll(root, 0o755); err != nil {
		t.Fatal(err)
	}
	keep := filepath.Join(root, "work.txt")
	if err := os.WriteFile(keep, []byte("a bot's world\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	ws := &Workspace{ID: "abc123", Root: root, Source: dir}

	if IsPersistent(root) {
		t.Fatal("workspace is persistent before being marked")
	}
	if err := MarkPersistent(ws); err != nil {
		t.Fatalf("MarkPersistent: %v", err)
	}
	if !IsPersistent(root) {
		t.Fatal("marker written but IsPersistent is false")
	}
	if !ws.Persistent {
		t.Fatal("MarkPersistent did not set the struct field")
	}

	m := New()
	err := m.Discard(ws)
	if err == nil {
		t.Fatal("Discard removed a persistent workspace without complaint")
	}
	if !strings.Contains(err.Error(), "Destroy") {
		t.Fatalf("error should point at Destroy, got: %v", err)
	}
	if _, statErr := os.Stat(keep); statErr != nil {
		t.Fatalf("persistent workspace contents were removed: %v", statErr)
	}

	if err := m.Destroy(ws); err != nil {
		t.Fatalf("Destroy: %v", err)
	}
	if _, statErr := os.Stat(root); !os.IsNotExist(statErr) {
		t.Fatal("Destroy left the workspace behind")
	}
}

// A non-persistent workspace must still be discardable, or every task leaks.
func TestDiscardStillRemovesOrdinary(t *testing.T) {
	dir := t.TempDir()
	root := filepath.Join(dir, "clone")
	if err := os.MkdirAll(root, 0o755); err != nil {
		t.Fatal(err)
	}
	ws := &Workspace{ID: "def456", Root: root, Source: dir}
	if err := New().Discard(ws); err != nil {
		t.Fatalf("Discard on an ordinary workspace: %v", err)
	}
	if _, err := os.Stat(root); !os.IsNotExist(err) {
		t.Fatal("ordinary workspace survived Discard")
	}
}
