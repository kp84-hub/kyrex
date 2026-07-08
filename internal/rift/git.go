package rift

import (
	"os/exec"
	"path/filepath"
	"strings"
)

// ChangeKind classifies a working-tree change.
type ChangeKind string

const (
	Added     ChangeKind = "added"
	Modified  ChangeKind = "modified"
	Deleted   ChangeKind = "deleted"
	Renamed   ChangeKind = "renamed"
	Untracked ChangeKind = "untracked"
)

// Change is a single path that differs from the workspace's HEAD.
type Change struct {
	Path string
	Kind ChangeKind
}

// MergeIgnoreNames are engine/orchestrator runtime artifacts that must never
// be merged back into the user's project. They are separate from
// DefaultIgnoreNames (which controls clone-time skipping) because these
// artifacts are created inside a workspace during an agent run and should
// be filtered from change detection and merge-back, not from cloning.
// Note: any path segment beginning with ".px" is also excluded to
// future-proof against new .px_* variants (e.g. ".pxignore-lookalike"
// is excluded by this rule).
var MergeIgnoreNames = []string{
	".px",
	".px_sessions",
	".px_history",
	".kx-lane",
}

// HasGit reports whether dir is inside a git working tree.
func HasGit(dir string) bool {
	cmd := exec.Command("git", "-C", dir, "rev-parse", "--is-inside-work-tree")
	out, err := cmd.Output()
	return err == nil && strings.TrimSpace(string(out)) == "true"
}

// ChangedFiles returns every path that differs from HEAD in dir, including
// untracked files. This is the ground-truth set the agent actually wrote — it
// does not trust any agent self-report.
func ChangedFiles(dir string) ([]Change, error) {
	cmd := exec.Command("git", "-C", dir, "status", "--porcelain", "--untracked-files=all")
	out, err := cmd.Output()
	if err != nil {
		return nil, err
	}
	var changes []Change
	for _, line := range strings.Split(string(out), "\n") {
		if len(line) < 4 {
			continue
		}
		code := line[:2]
		path := strings.TrimSpace(line[3:])
		// handle "old -> new" rename form: take the destination path.
		if i := strings.Index(path, " -> "); i >= 0 {
			path = path[i+4:]
		}
		path = strings.Trim(path, `"`)
		if isMergeIgnored(path) {
			continue
		}
		changes = append(changes, Change{Path: path, Kind: classify(code)})
	}
	return changes, nil
}

func classify(code string) ChangeKind {
	switch {
	case code == "??":
		return Untracked
	case strings.ContainsAny(code, "R"):
		return Renamed
	case strings.ContainsAny(code, "D"):
		return Deleted
	case strings.ContainsAny(code, "A"):
		return Added
	default:
		return Modified
	}
}

// Diff returns a unified diff of all tracked changes in dir against HEAD.
// Untracked files are listed separately by ChangedFiles.
func Diff(dir string) (string, error) {
	cmd := exec.Command("git", "-C", dir, "--no-pager", "diff", "HEAD")
	out, err := cmd.Output()
	if err != nil {
		return "", err
	}
	return string(out), nil
}

// cleanRel normalizes a possibly-quoted git path to a clean relative path.
func cleanRel(p string) string {
	return filepath.Clean(strings.Trim(p, `"`))
}

// isMergeIgnored reports whether any path segment of path matches a
// MergeIgnoreNames entry (exact match) or begins with ".px". Git porcelain
// output uses forward slashes, so splitting is always on "/". This catches
// engine artifacts at any nesting depth — e.g. ".px_sessions/main.json",
// "src/.px_sessions/x.json", ".px/config.json", and future .px_* variants.
func isMergeIgnored(path string) bool {
	for _, seg := range strings.Split(path, "/") {
		if seg == "" {
			continue
		}
		for _, name := range MergeIgnoreNames {
			if seg == name {
				return true
			}
		}
		if strings.HasPrefix(seg, ".px") {
			return true
		}
	}
	return false
}
