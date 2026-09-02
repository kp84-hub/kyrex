package rift

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestMergeFileContainment validates that the containment check in MergeFile
// correctly identifies paths inside vs outside the workspace, handling the
// real-world path representation quirks that the old strings.TrimPrefix
// approach could not (symlinks, trailing slashes, etc.).
func TestMergeFileContainment(t *testing.T) {
	m := New()

	// Build a fixed workspace structure under a temp dir.
	root := t.TempDir()
	subDir := filepath.Join(root, "subdir")
	if err := os.MkdirAll(subDir, 0o755); err != nil {
		t.Fatal(err)
	}
	insideFile := filepath.Join(subDir, "test.txt")
	if err := os.WriteFile(insideFile, []byte("hello"), 0o644); err != nil {
		t.Fatal(err)
	}

	// Create a symlink pointing to root for the symlink test case.
	symBase := t.TempDir()
	symRoot := filepath.Join(symBase, "symlink-root")
	if err := os.Symlink(root, symRoot); err != nil {
		t.Fatal(err)
	}
	symFile := filepath.Join(symRoot, "subdir", "test.txt")

	// Genuinely outside path: a file in a completely separate temp dir.
	outsideDir := t.TempDir()
	outsideFile := filepath.Join(outsideDir, "outside.txt")
	if err := os.WriteFile(outsideFile, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}

	source := t.TempDir()

	tests := []struct {
		name      string
		wsRoot    string
		clonePath string
		wantErr   bool
		errSubstr string // optional; if set error must contain this
	}{
		{
			name:      "identical paths normal form",
			wsRoot:    root,
			clonePath: insideFile,
			wantErr:   false,
		},
		{
			name:      "workspace root with trailing slash",
			wsRoot:    root + "/",
			clonePath: insideFile,
			wantErr:   false,
		},
		{
			name:      "symlink in clone path",
			wsRoot:    root,
			clonePath: symFile,
			wantErr:   false,
		},
		{
			name:      "symlinked workspace root",
			wsRoot:    symRoot,
			clonePath: insideFile,
			wantErr:   false,
		},
		{
			name:      "genuinely outside path",
			wsRoot:    root,
			clonePath: outsideFile,
			wantErr:   true,
			errSubstr: "not inside workspace",
		},
		{
			name:      "clone path equals workspace root",
			wsRoot:    root,
			clonePath: root,
			wantErr:   true,
			errSubstr: "not inside workspace",
		},
		{
			name:      "path above workspace root",
			wsRoot:    root,
			clonePath: filepath.Dir(root),
			wantErr:   true,
			errSubstr: "not inside workspace",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			ws := &Workspace{Root: tc.wsRoot, Source: source}
			err := m.MergeFile(ws, tc.clonePath)

			if tc.wantErr {
				if err == nil {
					t.Errorf("MergeFile(%q) = nil; want error", tc.clonePath)
				} else if tc.errSubstr != "" && !strings.Contains(err.Error(), tc.errSubstr) {
					t.Errorf("MergeFile(%q) error = %q; want it to contain %q", tc.clonePath, err, tc.errSubstr)
				}
				return
			}

			if err != nil {
				t.Errorf("MergeFile(%q) = %v; want nil", tc.clonePath, err)
			}
		})
	}
}

// TestDeleteFileContainment validates that the approved-deletion propagation
// (DeleteFile) applies the same containment protections as MergeFile:
// workspace-root deletion, outside paths, ignored paths and the literal
// display string ("DELETE: rm ...") are all rejected, while a deletion that
// actually happened in the clone is propagated to the real tree.
func TestDeleteFileContainment(t *testing.T) {
	m := New()

	root := t.TempDir()         // workspace clone
	source := t.TempDir()       // real tree

	write := func(path, content string) {
		t.Helper()
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}

	insideFile := filepath.Join(root, "file.txt")
	write(insideFile, "hello")
	write(filepath.Join(source, "file.txt"), "hello")

	nestedFile := filepath.Join(root, "subdir", "nested.txt")
	write(nestedFile, "n")
	write(filepath.Join(source, "subdir", "nested.txt"), "n")

	dirPath := filepath.Join(root, "tree")
	write(filepath.Join(dirPath, "leaf.txt"), "l")
	write(filepath.Join(source, "tree", "leaf.txt"), "l")

	outside := filepath.Join(t.TempDir(), "outside.txt")
	write(outside, "x")

	ignoredArtifact := filepath.Join(root, ".px_history")
	write(ignoredArtifact, "h")
	write(filepath.Join(source, ".px_history"), "h")

	ws := &Workspace{Root: root, Source: source}

	// 1. Approved propagation: engine already deleted the clone copy, so the
	//    real-tree file is removed.
	if err := os.Remove(insideFile); err != nil {
		t.Fatal(err)
	}
	if err := m.DeleteFile(ws, insideFile); err != nil {
		t.Fatalf("DeleteFile(inside) = %v; want nil", err)
	}
	if _, err := os.Stat(filepath.Join(source, "file.txt")); !os.IsNotExist(err) {
		t.Fatal("approved deletion was not propagated to the real tree")
	}

	// 2. Recursive dir deletion mirrors rm -r semantics for a validated dir.
	if err := os.RemoveAll(dirPath); err != nil {
		t.Fatal(err)
	}
	if err := m.DeleteFile(ws, dirPath); err != nil {
		t.Fatalf("DeleteFile(dir) = %v; want nil", err)
	}
	if _, err := os.Stat(filepath.Join(source, "tree")); !os.IsNotExist(err) {
		t.Fatal("approved directory deletion was not propagated to the real tree")
	}

	// 3. Clone copy still present → engine's rm did not remove it → no
	//    propagation, no error.
	if err := m.DeleteFile(ws, nestedFile); err != nil {
		t.Fatalf("DeleteFile(untouched) = %v; want nil skip", err)
	}
	if _, err := os.Stat(filepath.Join(source, "subdir", "nested.txt")); err != nil {
		t.Fatalf("source file should be untouched when the clone copy still exists: %v", err)
	}

	// 4. Workspace root is never deletable.
	if err := m.DeleteFile(ws, root); err == nil {
		t.Fatal("DeleteFile(workspace root) = nil; want error")
	}

	// 5. Existing path outside the workspace is rejected.
	if err := m.DeleteFile(ws, outside); err == nil {
		t.Fatal("DeleteFile(outside existing) = nil; want error")
	}

	// 6. Missing path outside the workspace is rejected.
	missingOutside := filepath.Join(t.TempDir(), "nope.txt")
	if err := m.DeleteFile(ws, missingOutside); err == nil {
		t.Fatal("DeleteFile(outside missing) = nil; want error")
	}

	// 7. The literal display string is never treated as a filesystem path.
	if err := m.DeleteFile(ws, "DELETE: rm "+insideFile); err == nil {
		t.Fatal("DeleteFile(display string) = nil; want error")
	}

	// 8. Merge-ignored artifact (.px_history) is rejected even when its clone
	//    copy is gone.
	if err := os.Remove(ignoredArtifact); err != nil {
		t.Fatal(err)
	}
	if err := m.DeleteFile(ws, ignoredArtifact); err == nil {
		t.Fatal("DeleteFile(merge-ignored) = nil; want error")
	}
	if _, err := os.Stat(filepath.Join(source, ".px_history")); err != nil {
		t.Fatalf("merge-ignored source file must survive: %v", err)
	}

	// 9. Clone-ignored path (node_modules) is rejected even if a matching file
	//    somehow exists in the real tree.
	if err := m.DeleteFile(ws, filepath.Join(root, "node_modules", "pkg.js")); err == nil {
		t.Fatal("DeleteFile(clone-ignored) = nil; want error")
	}

	// 10. Nowhere-exists path is a no-op, not a fabricated deletion.
	if err := m.DeleteFile(ws, filepath.Join(root, "never-existed.txt")); err != nil {
		t.Fatalf("DeleteFile(never-existed) = %v; want nil no-op", err)
	}
}