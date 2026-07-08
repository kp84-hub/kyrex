package rift

import "testing"

func TestIsMergeIgnored(t *testing.T) {
	tests := []struct {
		path string
		want bool
	}{
		// Engine artifacts at top level
		{path: ".px_sessions/main.json", want: true},
		{path: ".px_history", want: true},
		{path: ".kx-lane", want: true},
		{path: ".px/config.json", want: true},
		// Engine artifact nested in a subdirectory
		{path: "src/.px_sessions/x.json", want: true},
		// Non-ignored user files
		{path: "main.go", want: false},
		{path: "utils/helper.go", want: false},
		// Must NOT false-positive on names merely containing "px"
		{path: "pxutils/file.go", want: false},
		// .px-prefixed lookalike — the .px prefix rule catches this
		{path: ".pxignore-lookalike", want: true},
	}

	for _, tc := range tests {
		got := isMergeIgnored(tc.path)
		if got != tc.want {
			t.Errorf("isMergeIgnored(%q) = %v; want %v", tc.path, got, tc.want)
		}
	}
}
