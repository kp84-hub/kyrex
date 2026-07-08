package race

import (
	"os"
	"path/filepath"
	"testing"
)

func TestDefaultGateCommand_GoModPresent(t *testing.T) {
	dir := t.TempDir()

	// Create go.mod
	if err := os.WriteFile(filepath.Join(dir, "go.mod"), []byte("module test\n"), 0644); err != nil {
		t.Fatal(err)
	}

	cmd := DefaultGateCommand(dir)
	if cmd != "go build ./..." {
		t.Errorf("expected \"go build ./...\", got %q", cmd)
	}
}

func TestDefaultGateCommand_GoModAbsent(t *testing.T) {
	dir := t.TempDir()

	// No go.mod
	cmd := DefaultGateCommand(dir)
	if cmd != "true" {
		t.Errorf("expected \"true\", got %q", cmd)
	}
}

func TestDefaultGateCommand_NonexistentDir(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "nonexistent")

	cmd := DefaultGateCommand(dir)
	if cmd != "true" {
		t.Errorf("expected \"true\", got %q", cmd)
	}
}
