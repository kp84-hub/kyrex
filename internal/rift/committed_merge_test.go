package rift

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func commitFixture(t *testing.T, dir string) {
	t.Helper()
	for _, args := range [][]string{
		{"add", "-A"},
		{"-c", "maintenance.auto=false", "-c", "gc.auto=0", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture"},
	} {
		if out, err := exec.Command("git", append([]string{"-C", dir}, args...)...).CombinedOutput(); err != nil {
			t.Fatalf("git: %v: %s", err, out)
		}
	}
}

func committedFixture(t *testing.T) (*Manager, *Workspace) {
	t.Helper()
	source := t.TempDir()
	if err := os.WriteFile(filepath.Join(source, "app.txt"), []byte("original\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if out, err := exec.Command("git", "-C", source, "init", "-q").CombinedOutput(); err != nil {
		t.Fatalf("git init: %v: %s", err, out)
	}
	// A fresh fixture must not start asynchronous Git maintenance while the
	// manager is copying it; the test exercises committed merge detection.
	commitFixture(t, source)
	m := New()
	m.Storage = t.TempDir()
	ws, err := m.Create(source, "")
	if err != nil {
		t.Fatal(err)
	}
	return m, ws
}

func TestMergeBackCopiesCommittedChangesAndKeepsSourceHistory(t *testing.T) {
	m, ws := committedFixture(t)
	sourceHead, _ := exec.Command("git", "-C", ws.Source, "rev-parse", "HEAD").Output()
	os.WriteFile(filepath.Join(ws.Root, "app.txt"), []byte("new feature\n"), 0o644)
	os.WriteFile(filepath.Join(ws.Root, "new -> file.txt"), []byte("new file\n"), 0o644)
	commitFixture(t, ws.Root)
	status, _ := exec.Command("git", "-C", ws.Root, "status", "--porcelain").Output()
	if len(status) != 0 {
		t.Fatalf("repro requires a clean committed clone: %s", status)
	}
	diff, err := m.Diff(ws)
	if err != nil || !strings.Contains(diff, "+new feature") {
		t.Fatalf("committed change absent from review diff: %v %q", err, diff)
	}
	changes, err := m.MergeBack(ws)
	if err != nil || len(changes) != 2 {
		t.Fatalf("merge: %v, changes: %#v", err, changes)
	}
	for name, want := range map[string]string{"app.txt": "new feature\n", "new -> file.txt": "new file\n"} {
		data, err := os.ReadFile(filepath.Join(ws.Source, name))
		if err != nil || string(data) != want {
			t.Fatalf("original project %s = %q, %v; want %q", name, data, err, want)
		}
	}
	after, _ := exec.Command("git", "-C", ws.Source, "rev-parse", "HEAD").Output()
	if string(after) != string(sourceHead) {
		t.Fatal("file merge must not replace the source's Git history")
	}
	if pending, err := m.UnmergedChanges(ws); err != nil || len(pending) != 0 {
		t.Fatalf("applied bytes still pending: %#v, %v", pending, err)
	}
}

func TestLaterCommittedEditToAlreadyMergedFileRemainsPending(t *testing.T) {
	m, ws := committedFixture(t)
	path := filepath.Join(ws.Root, "app.txt")
	os.WriteFile(path, []byte("first\n"), 0o644)
	commitFixture(t, ws.Root)
	if err := m.MergeFile(ws, path); err != nil {
		t.Fatal(err)
	}
	os.WriteFile(path, []byte("second\n"), 0o644)
	commitFixture(t, ws.Root)
	pending, err := m.UnmergedChanges(ws)
	if err != nil || len(pending) != 1 || pending[0].Path != "app.txt" {
		t.Fatalf("later revision hidden by prior merge: %#v, %v", pending, err)
	}
	if _, err := m.MergeBack(ws); err != nil {
		t.Fatal(err)
	}
	got, _ := os.ReadFile(filepath.Join(ws.Source, "app.txt"))
	if string(got) != "second\n" {
		t.Fatalf("second revision did not reach project: %q", got)
	}
}

func TestFirstCommitInAnUnbornRiftIsMergeable(t *testing.T) {
	source := t.TempDir()
	if out, err := exec.Command("git", "-C", source, "init", "-q").CombinedOutput(); err != nil {
		t.Fatalf("git init: %v: %s", err, out)
	}
	m := New()
	m.Storage = t.TempDir()
	ws, err := m.Create(source, "")
	if err != nil {
		t.Fatal(err)
	}
	os.WriteFile(filepath.Join(ws.Root, "first.txt"), []byte("first feature\n"), 0o644)
	commitFixture(t, ws.Root)
	changes, err := m.MergeBack(ws)
	if err != nil || len(changes) != 1 {
		t.Fatalf("first commit merge: %#v, %v", changes, err)
	}
	got, _ := os.ReadFile(filepath.Join(source, "first.txt"))
	if string(got) != "first feature\n" {
		t.Fatalf("first committed file did not land: %q", got)
	}
}

func TestCommittedDeletionRemainsInMergeDiff(t *testing.T) {
	m, ws := committedFixture(t)
	os.Remove(filepath.Join(ws.Root, "app.txt"))
	commitFixture(t, ws.Root)
	changes, err := m.MergeBack(ws)
	if err != nil || len(changes) != 1 || changes[0].Kind != Deleted {
		t.Fatalf("deletion merge: %#v, %v", changes, err)
	}
	if _, err := os.Stat(filepath.Join(ws.Source, "app.txt")); !os.IsNotExist(err) {
		t.Fatalf("approved deletion did not reach project: %v", err)
	}
}

func TestBaselineMetadataDoesNotTouchSourceOrGetMerged(t *testing.T) {
	m, ws := committedFixture(t)
	if _, err := os.Stat(filepath.Join(ws.Root, ".git", baselineFile)); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(ws.Source, ".git", baselineFile)); !os.IsNotExist(err) {
		t.Fatalf("baseline leaked into original Git metadata: %v", err)
	}
	os.WriteFile(filepath.Join(ws.Root, ".px_history"), []byte("runtime"), 0o644)
	commitFixture(t, ws.Root)
	if changes, err := m.Changes(ws); err != nil || len(changes) != 0 {
		t.Fatalf("runtime artifact was offered for merge: %#v, %v", changes, err)
	}
}

func TestPinBaselineDoesNotFollowParentGitOrSymlinkMetadata(t *testing.T) {
	_, ws := committedFixture(t)
	subdir := filepath.Join(ws.Source, "nested")
	os.Mkdir(subdir, 0o755)
	if err := PinBaseline(subdir); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(ws.Source, ".git", baselineFile)); !os.IsNotExist(err) {
		t.Fatal("nested non-Git project modified parent metadata")
	}
	linkRoot := t.TempDir()
	os.Symlink(filepath.Join(ws.Source, ".git"), filepath.Join(linkRoot, ".git"))
	if err := PinBaseline(linkRoot); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(ws.Source, ".git", baselineFile)); !os.IsNotExist(err) {
		t.Fatal("symlink metadata modified the source")
	}
}

func TestInvalidBaselineDoesNotSilentlyHideCommittedChanges(t *testing.T) {
	m, ws := committedFixture(t)
	os.WriteFile(filepath.Join(ws.Root, "app.txt"), []byte("feature\n"), 0o644)
	commitFixture(t, ws.Root)
	if err := os.WriteFile(filepath.Join(ws.Root, ".git", baselineFile), []byte("invalid\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := m.UnmergedChanges(ws); err == nil {
		t.Fatal("invalid baseline reported a clean clone")
	}
	if _, err := m.MergeBack(ws); err == nil {
		t.Fatal("invalid baseline reported a successful merge")
	}
	got, _ := os.ReadFile(filepath.Join(ws.Source, "app.txt"))
	if string(got) != "original\n" {
		t.Fatalf("unverifiable change was applied: %q", got)
	}
}
