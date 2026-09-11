package rift

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestMergeFilesAllOrNothingRollsBack pins the command-write merge guarantee: a
// LATER target failing must roll back an EARLIER target that would otherwise
// have succeeded, leaving the real project byte-identical to before.
func TestMergeFilesAllOrNothingRollsBack(t *testing.T) {
	root := t.TempDir()   // clone
	source := t.TempDir() // real project
	ws := &Workspace{Root: root, Source: source}
	m := New()

	// Target 1: destination already exists — a successful merge would overwrite
	// it, so rollback must restore the original bytes.
	if err := os.WriteFile(filepath.Join(source, "pre.txt"), []byte("original\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "pre.txt"), []byte("changed\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	// Target 2: the destination path is a directory, so copyFile cannot write
	// it. This passes preflight (the clone source is a regular file) and fails
	// DURING the merge — exactly the late failure rollback must survive.
	if err := os.MkdirAll(filepath.Join(source, "blocked.txt"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "blocked.txt"), []byte("blocked\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	// Target 3: a brand-new file that must never be created once target 2 fails.
	if err := os.WriteFile(filepath.Join(root, "new.txt"), []byte("new\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	err := m.MergeFiles(ws, []string{
		filepath.Join(root, "pre.txt"),
		filepath.Join(root, "blocked.txt"),
		filepath.Join(root, "new.txt"),
	})
	if err == nil {
		t.Fatal("MergeFiles = nil; want error when a target cannot be merged")
	}

	// The earlier successful merge is rolled back to the original content.
	if data, rerr := os.ReadFile(filepath.Join(source, "pre.txt")); rerr != nil || string(data) != "original\n" {
		t.Fatalf("rollback did not restore pre.txt: err=%v content=%q", rerr, data)
	}
	// The never-reached target leaves nothing behind.
	if _, serr := os.Stat(filepath.Join(source, "new.txt")); !os.IsNotExist(serr) {
		t.Fatalf("new.txt should not exist after rollback: %v", serr)
	}
	// The pre-existing directory is untouched, not replaced by a file.
	if fi, serr := os.Stat(filepath.Join(source, "blocked.txt")); serr != nil || !fi.IsDir() {
		t.Fatalf("blocked.txt directory must survive: err=%v", serr)
	}
}

// TestMergeFilesPreflightRejectsBeforeWriting: a bad target (outside the
// workspace) is caught in preflight so an EARLIER valid target is never written.
func TestMergeFilesPreflightRejectsBeforeWriting(t *testing.T) {
	root := t.TempDir()
	source := t.TempDir()
	ws := &Workspace{Root: root, Source: source}
	m := New()

	if err := os.WriteFile(filepath.Join(root, "ok.txt"), []byte("ok\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	outside := filepath.Join(t.TempDir(), "outside.txt")
	if err := os.WriteFile(outside, []byte("x\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	err := m.MergeFiles(ws, []string{filepath.Join(root, "ok.txt"), outside})
	if err == nil {
		t.Fatal("MergeFiles = nil; want a preflight containment error")
	}
	if !strings.Contains(err.Error(), "not inside workspace") {
		t.Fatalf("error = %q; want a containment failure", err)
	}
	if _, serr := os.Stat(filepath.Join(source, "ok.txt")); !os.IsNotExist(serr) {
		t.Fatal("preflight must reject before writing any target")
	}
}

// TestMergeFilesSuccessMergesAll is the positive control.
func TestMergeFilesSuccessMergesAll(t *testing.T) {
	root := t.TempDir()
	source := t.TempDir()
	ws := &Workspace{Root: root, Source: source}
	m := New()

	if err := os.WriteFile(filepath.Join(root, "a.txt"), []byte("A\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(root, "sub"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "sub", "b.txt"), []byte("B\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	if err := m.MergeFiles(ws, []string{
		filepath.Join(root, "a.txt"),
		filepath.Join(root, "sub", "b.txt"),
	}); err != nil {
		t.Fatalf("MergeFiles = %v; want nil", err)
	}
	if data, err := os.ReadFile(filepath.Join(source, "a.txt")); err != nil || string(data) != "A\n" {
		t.Fatalf("a.txt not merged: %v %q", err, data)
	}
	if data, err := os.ReadFile(filepath.Join(source, "sub", "b.txt")); err != nil || string(data) != "B\n" {
		t.Fatalf("sub/b.txt not merged: %v %q", err, data)
	}
}

// TestMergeFilesPreflightRejectsMissingSource: a ghost clone path is rejected in
// preflight (no partial write), matching the single-file MergeFile failure the
// live gate used to surface only after starting the merge.
func TestMergeFilesPreflightRejectsMissingSource(t *testing.T) {
	root := t.TempDir()
	source := t.TempDir()
	ws := &Workspace{Root: root, Source: source}
	m := New()

	err := m.MergeFiles(ws, []string{filepath.Join(root, "ghost.txt")})
	if err == nil {
		t.Fatal("MergeFiles = nil; want error for a missing clone source")
	}
	if _, serr := os.Stat(filepath.Join(source, "ghost.txt")); !os.IsNotExist(serr) {
		t.Fatal("nothing should be written for a missing source")
	}
}
