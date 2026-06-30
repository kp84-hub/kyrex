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
