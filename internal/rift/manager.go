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
	// Persistent workspaces belong to a Bot and are never swept or discarded.
	// The authority is the on-disk marker, not this field: the startup sweep
	// runs with directory paths and no Workspace values, so persistence has to
	// be visible on disk or the sweep cannot honour it.
	Persistent bool
}

// PersistentMarker names the file whose presence makes a workspace durable.
const PersistentMarker = ".rift-persistent"

// IsPersistent reports whether a clone directory is marked durable. It takes
// a path rather than a *Workspace so the startup sweep can consult it.
func IsPersistent(root string) bool {
	_, err := os.Stat(filepath.Join(root, PersistentMarker))
	return err == nil
}

// MarkPersistent makes a workspace durable: exempt from Discard and from the
// startup sweep. Used for Bot workspaces, whose whole value is surviving the
// runtime that created them.
func MarkPersistent(ws *Workspace) error {
	if ws == nil || ws.Root == "" {
		return fmt.Errorf("rift: cannot mark a nil workspace persistent")
	}
	path := filepath.Join(ws.Root, PersistentMarker)
	if err := os.WriteFile(path, []byte(ws.ID+"\n"), 0o644); err != nil {
		return fmt.Errorf("rift: marking %q persistent: %w", ws.Root, err)
	}
	ws.Persistent = true
	return nil
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
// Discard removes a workspace. It refuses to remove a persistent one: a Bot
// workspace is its durable state, and deleting it silently would destroy
// exactly what the Bot exists to keep. Use Destroy for that, deliberately.
func (m *Manager) Discard(ws *Workspace) error {
	if ws == nil || ws.Root == "" {
		return nil
	}
	if IsPersistent(ws.Root) {
		return fmt.Errorf(
			"rift: refusing to discard persistent workspace %q - use Destroy",
			ws.Root)
	}
	return os.RemoveAll(ws.Root)
}

// Destroy removes a workspace even if it is persistent. This is the only way
// to delete a Bot's world, and callers should treat it as irreversible.
func (m *Manager) Destroy(ws *Workspace) error {
	if ws == nil || ws.Root == "" {
		return nil
	}
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

// DeleteFile propagates an approved deletion from the workspace clone to the
// source project, given the deleted path's absolute path inside the clone.
//
// It is the deletion counterpart to MergeFile and mirrors its containment
// guarantees:
//   - Both paths are canonicalized (EvalSymlinks; a missing clone target falls
//     back to lexical cleaning because the engine already removed it).
//   - filepath.Rel containment rejects anything outside the workspace and the
//     workspace root itself.
//   - Clone-ignored and merge-ignored paths are rejected: they were never part
//     of the clone's working set, so an approved rm there cannot represent a
//     real-tree deletion.
//   - If the target still exists in the clone, the engine's rm did not remove
//     it (failed, or never executed), so nothing is propagated.
//
// Recursive removal (os.RemoveAll) is used only when the validated destination
// target is itself a directory — the rm -r semantics the operator approved.
func (m *Manager) DeleteFile(ws *Workspace, clonePath string) error {
	if ws == nil || ws.Root == "" || ws.Source == "" {
		return fmt.Errorf("rift: nil or empty workspace in DeleteFile")
	}
	cleanRoot, err := filepath.EvalSymlinks(ws.Root)
	if err != nil {
		return fmt.Errorf("rift: resolving workspace root %q: %w", ws.Root, err)
	}

	cleanPath, err := filepath.EvalSymlinks(clonePath)
	if err != nil {
		// The target no longer resolves — the engine deleted it (or it never
		// existed). Fall back to lexical cleaning; the containment and
		// remove/RemoveAll semantics below stay bounded to ws.Source.
		cleanPath = filepath.Clean(clonePath)
	}

	rel, err := filepath.Rel(cleanRoot, cleanPath)
	if err != nil {
		return fmt.Errorf("rift: could not compute relative path from %q to %q: %w", clonePath, ws.Root, err)
	}
	// "." means clonePath resolved to exactly ws.Root — the root may never be
	// deleted. ".." prefix means genuinely outside the workspace.
	if rel == "." || strings.HasPrefix(rel, "..") {
		return fmt.Errorf("rift: %q is not inside workspace %q", clonePath, ws.Root)
	}
	// Paths that were never in the clone (or are orchestrator artifacts) cannot
	// have been deleted by an approved rm; refusing avoids deleting a real-tree
	// file the engine could never have touched (e.g. clone-ignored dirs).
	if m.anySegmentIgnored(rel) || isMergeIgnored(rel) {
		return fmt.Errorf("rift: refusing to delete ignored path %q", rel)
	}

	// The engine runs rm only after approval. If the clone copy still exists,
	// rm did not remove it — do not fabricate a real-tree deletion.
	if _, statErr := os.Lstat(cleanPath); statErr == nil {
		return nil
	}

	dstPath := filepath.Join(ws.Source, rel)
	fi, err := os.Lstat(dstPath)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	if fi.IsDir() {
		if err := os.RemoveAll(dstPath); err != nil {
			return err
		}
	} else if err := os.Remove(dstPath); err != nil {
		return err
	}
	if _, err := os.Lstat(dstPath); err == nil {
		return fmt.Errorf("rift: deletion of %q did not take", dstPath)
	} else if !os.IsNotExist(err) {
		return err
	}
	return nil
}

// anySegmentIgnored reports whether any path segment of rel matches the
// clone-time ignore set (directory names such as node_modules or .venv). A
// path beneath an ignored directory was never part of the clone, so an
// approved rm there could never have deleted a real-tree copy.
func (m *Manager) anySegmentIgnored(rel string) bool {
	for _, seg := range strings.Split(rel, string(os.PathSeparator)) {
		if seg == "" {
			continue
		}
		if m.ignore().Match(seg, false) {
			return true
		}
	}
	return false
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
