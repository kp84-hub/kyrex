package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/kp84-hub/kx/internal/rift"
)

// The sweep deletes stale clones. A Bot workspace looks stale whenever the Bot
// has been idle, so without the marker check the sweep is a Bot-eating loop.
func TestSweepSkipsPersistent(t *testing.T) {
	storage := t.TempDir()
	old := time.Now().Add(-72 * time.Hour)

	ordinary := filepath.Join(storage, "ordinary")
	botWs := filepath.Join(storage, "botworld")
	for _, d := range []string{ordinary, botWs} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	keep := filepath.Join(botWs, "work.txt")
	if err := os.WriteFile(keep, []byte("durable\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	marker := filepath.Join(botWs, rift.PersistentMarker)
	if err := os.WriteFile(marker, []byte("botworld\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	for _, d := range []string{ordinary, botWs} {
		if err := os.Chtimes(d, old, old); err != nil {
			t.Fatal(err)
		}
	}

	removed, _ := sweepStaleRifts(storage, 24*time.Hour)

	if _, err := os.Stat(ordinary); !os.IsNotExist(err) {
		t.Fatal("stale ordinary workspace survived the sweep")
	}
	if _, err := os.Stat(keep); err != nil {
		t.Fatalf("sweep deleted a persistent Bot workspace: %v", err)
	}
	if removed != 1 {
		t.Fatalf("expected 1 removal, got %d", removed)
	}
}
