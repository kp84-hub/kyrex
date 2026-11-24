package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/kp84-hub/kx/internal/rift"
)

func TestCleanupRetainsCommittedWorkUntilProjectFilesMatch(t *testing.T) {
	for _, applied := range []bool{false, true} {
		t.Run(map[bool]string{false: "unmerged", true: "applied"}[applied], func(t *testing.T) {
			cleanupOnce = sync.Once{}
			t.Cleanup(func() { cleanupOnce = sync.Once{} })
			source := t.TempDir()
			git := func(dir string, args ...string) {
				t.Helper()
				if out, err := exec.Command("git", append([]string{"-C", dir, "-c", "maintenance.auto=false", "-c", "gc.auto=0"}, args...)...).CombinedOutput(); err != nil {
					t.Fatalf("git: %v: %s", err, out)
				}
			}
			os.WriteFile(filepath.Join(source, "app.txt"), []byte("original\n"), 0o644)
			git(source, "init", "-q")
			git(source, "add", "-A")
			git(source, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "initial")
			mgr := rift.New()
			mgr.Storage = t.TempDir()
			ws, err := mgr.Create(source, "")
			if err != nil {
				t.Fatal(err)
			}
			os.WriteFile(filepath.Join(ws.Root, "app.txt"), []byte("feature\n"), 0o644)
			git(ws.Root, "add", "-A")
			git(ws.Root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "feature")
			if applied {
				if _, err := mgr.MergeBack(ws); err != nil {
					t.Fatal(err)
				}
			}
			discardWorkspace(mgr, ws)
			if applied {
				if _, err := os.Stat(ws.Root); !os.IsNotExist(err) {
					t.Fatalf("fully applied clone was not cleaned: %v", err)
				}
				got, _ := os.ReadFile(filepath.Join(source, "app.txt"))
				if string(got) != "feature\n" {
					t.Fatalf("source lost approved content: %q", got)
				}
				return
			}
			if !rift.IsPersistent(ws.Root) {
				t.Fatal("unmerged committed draft was not protected")
			}
			old := time.Now().Add(-72 * time.Hour)
			os.Chtimes(ws.Root, old, old)
			if removed, _ := sweepStaleRifts(mgr.Storage, 24*time.Hour); removed != 0 {
				t.Fatal("startup cleanup removed a retained draft")
			}
			got, _ := os.ReadFile(filepath.Join(ws.Root, "app.txt"))
			if string(got) != "feature\n" {
				t.Fatalf("recovery bytes lost: %q", got)
			}
		})
	}
}
