package rift

import (
	"encoding/hex"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
)

// baselineFile lives only in the clone's Git metadata. Commits and branch
// switches must not move the revision against which Rift changes are reviewed.
const baselineFile = "kyrex-rift-base"

func isolatedGitMetadata(dir string) bool {
	info, err := os.Lstat(filepath.Join(dir, ".git"))
	if err != nil || !info.IsDir() {
		return false
	}
	// A copied worktree or shared commondir must not write into the source's
	// Git metadata. These layouts retain the existing legacy behavior.
	_, err = os.Lstat(filepath.Join(dir, ".git", "commondir"))
	return os.IsNotExist(err)
}

// PinBaseline records the clone's starting tree without changing HEAD, the
// index, source files, or any user branch. Unborn repositories use an empty
// tree so their first commit is reviewable too. Non-Git projects stay non-Git.
func PinBaseline(dir string) error {
	if !isolatedGitMetadata(dir) || !HasGit(dir) {
		return nil
	}
	cmd := exec.Command("git", "-C", dir, "rev-parse", "--verify", "HEAD^{tree}")
	out, err := cmd.Output()
	if err != nil {
		cmd = exec.Command("git", "-C", dir, "hash-object", "-t", "tree", "-w", "--stdin")
		out, err = cmd.Output()
		if err != nil {
			return fmt.Errorf("rift: cannot record starting tree: %w", err)
		}
	}
	path := filepath.Join(dir, ".git", baselineFile)
	if info, err := os.Lstat(path); err == nil && !info.Mode().IsRegular() {
		return fmt.Errorf("rift: refusing non-regular baseline metadata")
	} else if err != nil && !os.IsNotExist(err) {
		return err
	}
	return os.WriteFile(path, []byte(strings.TrimSpace(string(out))+"\n"), 0o600)
}

func pinnedBaseline(dir string) (string, error) {
	if !isolatedGitMetadata(dir) {
		return "", nil
	}
	path := filepath.Join(dir, ".git", baselineFile)
	info, err := os.Lstat(path)
	if os.IsNotExist(err) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	if !info.Mode().IsRegular() {
		return "", fmt.Errorf("rift: refusing non-regular baseline metadata")
	}
	out, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	base := strings.TrimSpace(string(out))
	if len(base) != 40 && len(base) != 64 {
		return "", fmt.Errorf("rift: invalid starting tree metadata")
	}
	if _, err := hex.DecodeString(base); err == nil {
		return base, nil
	}
	return "", fmt.Errorf("rift: invalid starting tree metadata")
}

func changesSince(dir, base string) ([]Change, error) {
	cmd := exec.Command("git", "-C", dir, "diff", "--name-status", "--no-renames", "-z", base, "--")
	out, err := cmd.Output()
	if err != nil {
		return nil, err
	}
	fields := strings.Split(strings.TrimSuffix(string(out), "\x00"), "\x00")
	paths := map[string]Change{}
	if len(out) > 0 {
		if len(fields)%2 != 0 {
			return nil, fmt.Errorf("rift: incomplete Git change record")
		}
		for i := 0; i < len(fields); i += 2 {
			path := fields[i+1]
			if !isMergeIgnored(path) {
				paths[path] = Change{Path: path, Kind: classify(fields[i])}
			}
		}
	}
	cmd = exec.Command("git", "-C", dir, "ls-files", "--others", "--exclude-standard", "-z")
	out, err = cmd.Output()
	if err != nil {
		return nil, err
	}
	for _, path := range strings.Split(string(out), "\x00") {
		if path != "" && !isMergeIgnored(path) {
			paths[path] = Change{Path: path, Kind: Untracked}
		}
	}
	changes := make([]Change, 0, len(paths))
	for _, change := range paths {
		changes = append(changes, change)
	}
	sort.Slice(changes, func(i, j int) bool { return changes[i].Path < changes[j].Path })
	return changes, nil
}

// ChangeKind classifies a working-tree change.
type ChangeKind string

const (
	Added     ChangeKind = "added"
	Modified  ChangeKind = "modified"
	Deleted   ChangeKind = "deleted"
	Renamed   ChangeKind = "renamed"
	Untracked ChangeKind = "untracked"
)

// Change is a single path that differs from the workspace's starting tree.
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

// ChangedFiles returns every path that differs from the pinned starting tree
// (HEAD for legacy workspaces without a pin), including
// untracked files. This is the ground-truth set the agent actually wrote — it
// does not trust any agent self-report.
func ChangedFiles(dir string) ([]Change, error) {
	base, err := pinnedBaseline(dir)
	if err != nil {
		return nil, err
	}
	if base != "" {
		return changesSince(dir, base)
	}
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

// Diff returns tracked changes against the starting tree (HEAD in legacy workspaces).
// Untracked files are listed separately by ChangedFiles.
func Diff(dir string) (string, error) {
	base, err := pinnedBaseline(dir)
	if err != nil {
		return "", err
	}
	if base == "" {
		base = "HEAD"
	}
	cmd := exec.Command("git", "-C", dir, "--no-pager", "diff", base)
	out, err := cmd.Output()
	if err != nil {
		return "", err
	}
	return string(out), nil
}

// runGit runs a git subcommand in dir and returns a descriptive error on
// failure. Used by the clone-revert path so a restore that did not take is
// reported rather than silently swallowed.
func runGit(dir string, args ...string) error {
	cmd := exec.Command("git", append([]string{"-C", dir}, args...)...)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("git %v: %v: %s", args, err, strings.TrimSpace(string(out)))
	}
	return nil
}

// gitStatusFor returns the porcelain status line for a single repo-relative
// path ("" when clean). It lets RevertFile distinguish an untracked leftover
// (remove it) from a tracked change (restore it).
func gitStatusFor(dir, rel string) (string, error) {
	cmd := exec.Command("git", "-C", dir, "status", "--porcelain", "--", rel)
	out, err := cmd.Output()
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(out)), nil
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
