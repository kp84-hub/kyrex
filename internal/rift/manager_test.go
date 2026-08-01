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