package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/kp84-hub/kx/internal/rift"
)

// TestBotWorkspaceIsPersistentAndSurvivesSweep verifies that a workspace
// created by the "kx bot create" path is marked persistent and that the
// startup sweep leaves it alone even when backdated past the cutoff.
func TestBotWorkspaceIsPersistentAndSurvivesSweep(t *testing.T) {
	source := t.TempDir()
	if err := os.WriteFile(filepath.Join(source, "work.txt"), []byte("content"), 0o644); err != nil {
		t.Fatal(err)
	}

	storage := t.TempDir()
	mgr := rift.New()
	mgr.Storage = storage

	ws, err := mgr.Create(source, "test-bot")
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	if ws.Root == source {
		t.Fatal("Create returned source path instead of a clone")
	}
	if ws.Name != "test-bot" {
		t.Fatalf("expected Name=test-bot, got %q", ws.Name)
	}

	// Mark persistent — exactly what runBotCreate does.
	if err := rift.MarkPersistent(ws); err != nil {
		t.Fatalf("MarkPersistent: %v", err)
	}
	if !rift.IsPersistent(ws.Root) {
		t.Fatal("IsPersistent is false after MarkPersistent")
	}

	// Backdate the workspace to well past the sweep cutoff.
	old := time.Now().Add(-72 * time.Hour)
	for _, d := range []string{ws.Root, filepath.Join(ws.Root, ".git")} {
		if fi, err := os.Stat(d); err == nil && fi.IsDir() {
			if err := os.Chtimes(d, old, old); err != nil {
				t.Fatal(err)
			}
		}
	}

	// The sweep should skip the persistent workspace even though it looks stale.
	removed, freed := sweepStaleRifts(storage, 24*time.Hour)
	if removed != 0 {
		t.Fatalf("sweep removed %d workspace(s) (freed %d bytes); expected 0", removed, freed)
	}
	if _, err := os.Stat(ws.Root); err != nil {
		t.Fatalf("persistent Bot workspace was deleted by sweep: %v", err)
	}
	if _, err := os.Stat(filepath.Join(ws.Root, "work.txt")); err != nil {
		t.Fatalf("contents of persistent workspace lost: %v", err)
	}

	// Verify the marker file is still there.
	if !rift.IsPersistent(ws.Root) {
		t.Fatal("workspace lost its persistent marker during sweep")
	}
}
