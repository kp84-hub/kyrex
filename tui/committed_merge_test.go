package tui

import (
	"os"
	"path/filepath"
	"testing"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/kp84-hub/kx/internal/rift"
)

func TestCommittedRiftFileReachesOriginalAfterSweepApproval(t *testing.T) {
	source := t.TempDir()
	os.WriteFile(filepath.Join(source, "app.txt"), []byte("original\n"), 0o644)
	gitCmd(t, source, "init", "-q")
	gitCmd(t, source, "add", "-A")
	gitCmd(t, source, "-c", "maintenance.auto=false", "-c", "gc.auto=0", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "initial")
	mgr := rift.New()
	mgr.Storage = t.TempDir()
	ws, err := mgr.Create(source, "")
	if err != nil {
		t.Fatal(err)
	}
	m := newApprovalTestModel(nil)
	m.AutoApprove = false
	m.Workspace, m.WorkspaceMgr = ws, mgr
	path := filepath.Join(ws.Root, "app.txt")
	for _, content := range []string{"first feature\n", "later feature\n"} {
		os.WriteFile(path, []byte(content), 0o644)
		gitCmd(t, ws.Root, "add", "-A")
		gitCmd(t, ws.Root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "feature")
		if !m.detectUnmergedChanges() || !m.SweepActive {
			t.Fatal("committed change disappeared from the real merge gate")
		}
		m, _, _ = m.handleSweepKey(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'y'}})
		got, _ := os.ReadFile(filepath.Join(source, "app.txt"))
		if string(got) != content {
			t.Fatalf("approval did not write original file: got %q, want %q", got, content)
		}
		m = m.markHandled(path) // A prior approval cannot hide later bytes.
		if m.detectUnmergedChanges() {
			t.Fatal("already applied file was offered again")
		}
	}
}

func TestCommittedFallbackRespectsPreExistingDirtyFiles(t *testing.T) {
	source, clone := makeGitTree(t)
	if err := rift.PinBaseline(clone); err != nil {
		t.Fatal(err)
	}
	m := newApprovalTestModel(nil)
	m.AutoApprove = false
	m.Workspace = &rift.Workspace{Root: clone, Source: source}
	m.WorkspaceMgr = rift.New()
	m.SweepBaseline = map[string]bool{"keep.txt": true}
	os.WriteFile(filepath.Join(clone, "keep.txt"), []byte("pre-existing file modified\n"), 0o644)
	gitCmd(t, clone, "add", "-A")
	gitCmd(t, clone, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "feature")
	if m.detectUnmergedChanges() || m.SweepActive {
		t.Fatal("pre-existing dirty file was offered for automatic fallback merge")
	}
	got, _ := os.ReadFile(filepath.Join(source, "keep.txt"))
	if string(got) != "base\n" {
		t.Fatalf("operator's source file was overwritten: %q", got)
	}
}
