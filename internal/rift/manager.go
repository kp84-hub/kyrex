package rift

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// Workspace is one cloned copy of a source project.
type Workspace struct {
	ID      string // short unique id
	Name    string // human label (e.g. "parser-fix"), optional
	Root    string // absolute path to the clone — set this as the engine's WORKSPACE_ROOT
	Source  string // absolute path to the original project
	Backend string // "reflink" or "copy"
	Created time.Time
}

// Manager creates and tracks workspaces for a set of source projects.
type Manager struct {
	// Storage, if set, is where clones are written. Leave empty to default to a
	// sibling ".rifts/<project>" directory next to each source — which keeps the
	// clone on the same filesystem as the source so reflinks can work.
	Storage string
	// Ignore controls which paths are skipped. Nil uses DefaultIgnoreNames.
	Ignore *Ignore
}

// New returns a Manager with default ignore rules.
func New() *Manager { return &Manager{Ignore: NewIgnore(nil)} }

func (m *Manager) ignore() *Ignore {
	if m.Ignore != nil {
		return m.Ignore
	}
	return NewIgnore(nil)
}

// storageFor returns the directory that will hold clones of source.
func (m *Manager) storageFor(source string) string {
	if m.Storage != "" {
		return m.Storage
	}
	return filepath.Join(filepath.Dir(source), ".rifts", filepath.Base(source))
}

// Create clones source into a fresh workspace and returns it. It selects the
// reflink backend when the storage filesystem supports it, else a plain copy.
func (m *Manager) Create(source, name string) (*Workspace, error) {
	source, err := filepath.Abs(source)
	if err != nil {
		return nil, err
	}
	// WalkDir lstats its root: a symlinked source is not a directory to it, so
	// the walk never descends and Clone returns nil having copied nothing.
	if resolved, rerr := filepath.EvalSymlinks(source); rerr == nil {
		source = resolved
	}
	if fi, err := os.Stat(source); err != nil || !fi.IsDir() {
		return nil, fmt.Errorf("rift: source %q is not a directory", source)
	}
	storage := m.storageFor(source)
	if err := os.MkdirAll(storage, 0o755); err != nil {
		return nil, err
	}
	id := newID()
	leaf := id
	if name != "" {
		leaf = name + "-" + id
	}
	root := filepath.Join(storage, leaf)
	if _, err := os.Stat(root); err == nil {
		return nil, fmt.Errorf("rift: workspace %q already exists", root)
	}

	backend := DetectBackend(storage)
	if err := backend.Clone(source, root, m.ignore()); err != nil {
		_ = os.RemoveAll(root)
		return nil, fmt.Errorf("rift: clone failed (%s backend): %w", backend.Name(), err)
	}

	// A clone that copies nothing is the worst failure mode: the engine starts
	// in an empty workspace, finds no config, and silently loads no model.
	if entries, derr := os.ReadDir(root); derr == nil && len(entries) == 0 {
		if srcEntries, serr := os.ReadDir(source); serr == nil && len(srcEntries) > 0 {
			_ = os.RemoveAll(root)
			return nil, fmt.Errorf("rift: clone produced an empty workspace from non-empty source %q (%s backend)", source, backend.Name())
		}
	}

	return &Workspace{
		ID:      id,
		Name:    name,
		Root:    root,
		Source:  source,
		Backend: backend.Name(),
		Created: time.Now(),
	}, nil
}

// Changes lists what the agent wrote in the workspace, computed from disk.
func (m *Manager) Changes(ws *Workspace) ([]Change, error) {
	if !HasGit(ws.Root) {
		return nil, fmt.Errorf("rift: %q is not a git repo; diff verification unavailable", ws.Root)
	}
	return ChangedFiles(ws.Root)
}

// Diff returns a unified diff of tracked changes in the workspace.
func (m *Manager) Diff(ws *Workspace) (string, error) {
	return Diff(ws.Root)
}

// MergeBack applies the workspace's changes onto the original source. Approve
// path. Modified/added/untracked files are copied back; deletions are applied.
// Returns the list of changes that were merged.
func (m *Manager) MergeBack(ws *Workspace) ([]Change, error) {
	if fi, err := os.Stat(ws.Source); err != nil || !fi.IsDir() {
		return nil, fmt.Errorf("rift: merge target %q is not a directory", ws.Source)
	}
	changes, err := m.Changes(ws)
	if err != nil {
		return nil, err
	}
	var merged []Change
	var failures []string
	for _, c := range changes {
		rel := cleanRel(c.Path)
		srcPath := filepath.Join(ws.Root, rel)
		dstPath := filepath.Join(ws.Source, rel)
		if c.Kind == Deleted {
			if err := os.Remove(dstPath); err != nil && !os.IsNotExist(err) {
				failures = append(failures, fmt.Sprintf("%s: %v", rel, err))
				continue
			}
			merged = append(merged, c)
			continue
		}
		if err := os.MkdirAll(filepath.Dir(dstPath), 0o755); err != nil {
			failures = append(failures, fmt.Sprintf("%s: %v", rel, err))
			continue
		}
		if err := copyFile(srcPath, dstPath); err != nil {
			failures = append(failures, fmt.Sprintf("%s: %v", rel, err))
			continue
		}
		// Same reasoning as MergeFile: a copy that succeeds is not necessarily
		// a copy of the right bytes. Report a drifted merge rather than
		// counting it as merged.
		if err := verifyCopy(srcPath, dstPath); err != nil {
			failures = append(failures, fmt.Sprintf("%s: %v", rel, err))
			continue
		}
		merged = append(merged, c)
	}
	if len(failures) > 0 {
		return merged, fmt.Errorf("rift: %d of %d changes failed to merge: %s", len(failures), len(changes), strings.Join(failures, "; "))
	}
	return merged, nil
}

// Discard deletes the workspace. Reject path.
func (m *Manager) Discard(ws *Workspace) error {
	return os.RemoveAll(ws.Root)
}

func newID() string {
	b := make([]byte, 4)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// MergeFile copies a single file from the workspace clone back to the
// source project, given the file's absolute path inside the clone.
// This avoids scanning the whole tree (and any unrelated git noise)
// when only one specific approved edit needs to be applied.
func (m *Manager) MergeFile(ws *Workspace, clonePath string) error {
	// Canonicalize both paths via EvalSymlinks so that symlinks, trailing
	// slashes, and other textual inconsistencies do not produce false
	// "not inside workspace" rejections.  filepath.EvalSymlinks also
	// applies filepath.Clean internally.
	cleanRoot, err := filepath.EvalSymlinks(ws.Root)
	if err != nil {
		return fmt.Errorf("rift: resolving workspace root %q: %w", ws.Root, err)
	}
	cleanPath, err := filepath.EvalSymlinks(clonePath)
	if err != nil {
		return fmt.Errorf("rift: resolving clone path %q: %w", clonePath, err)
	}

	// filepath.Rel provides a robust containment check: it returns a
	// relative path with no ".." components when clonePath is genuinely
	// inside cleanRoot.
	rel, err := filepath.Rel(cleanRoot, cleanPath)
	if err != nil {
		return fmt.Errorf("rift: could not compute relative path from %q to %q: %w", clonePath, ws.Root, err)
	}

	// "." means clonePath resolved to exactly ws.Root — treat as error
	// to preserve the original TrimPrefix-based behaviour.
	// ".." prefix means genuinely outside the workspace.
	if rel == "." || strings.HasPrefix(rel, "..") {
		return fmt.Errorf("rift: %q is not inside workspace %q", clonePath, ws.Root)
	}

	dstPath := filepath.Join(ws.Source, rel)
	if err := os.MkdirAll(filepath.Dir(dstPath), 0o755); err != nil {
		return err
	}
	if err := copyFile(clonePath, dstPath); err != nil {
		return err
	}
	// A successful copy is not the same as a correct one. The TUI calls this
	// after a fixed sleep rather than on a signal that the engine finished
	// writing, so copyFile can faithfully copy pre-write content and report
	// success. Compare the two files and fail loudly if they differ, rather
	// than telling the operator a change landed when it did not.
	return verifyCopy(clonePath, dstPath)
}

// verifyCopy reports an error if src and dst do not have identical contents.
func verifyCopy(src, dst string) error {
	srcSum, err := fileSum(src)
	if err != nil {
		return fmt.Errorf("rift: verifying merge source %q: %w", src, err)
	}
	dstSum, err := fileSum(dst)
	if err != nil {
		return fmt.Errorf("rift: verifying merge destination %q: %w", dst, err)
	}
	if srcSum != dstSum {
		return fmt.Errorf(
			"rift: merge of %q did not take - destination content differs "+
				"from the clone. The engine was probably still writing when "+
				"the merge ran; the approved change has NOT been applied", dst)
	}
	return nil
}

func fileSum(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}
