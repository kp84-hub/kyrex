// Package rift provides instant, space-efficient workspace clones for Kyrex
// agent runs. It uses filesystem copy-on-write (reflinks) when available and
// falls back to a plain recursive copy everywhere else, so the same API works
// on btrfs/XFS/APFS (CoW) and on ext4/WSL2/Windows (copy).
//
// The intended use is agent isolation: clone the project, point the engine's
// WORKSPACE_ROOT at the clone, let the agent edit freely, then compute the diff
// from disk (ground truth, not the agent's self-report) and either merge it
// back or discard it.
package rift

import (
	"io/fs"
	"os"
	"path/filepath"
	"strings"
)

// Backend clones a directory tree from src to dst.
type Backend interface {
	// Name reports the strategy in use ("reflink" or "copy").
	Name() string
	// Clone copies src into dst, skipping anything matched by ig.
	Clone(src, dst string, ig *Ignore) error
}

// DetectBackend probes storageDir's filesystem and returns the best backend.
// reflink requires the clone to live on the SAME filesystem as the source,
// which is why detection probes the storage directory rather than /tmp.
func DetectBackend(storageDir string) Backend {
	if err := probeReflink(storageDir); err == nil {
		return reflinkBackend{}
	}
	return copyBackend{}
}

// reflinkBackend clones each regular file via the platform CoW primitive,
// falling back to a byte copy for any file the kernel refuses (e.g. across
// devices). Detection should already have confirmed reflink works here.
type reflinkBackend struct{}

func (reflinkBackend) Name() string { return "reflink" }
func (reflinkBackend) Clone(src, dst string, ig *Ignore) error {
	return cloneTree(src, dst, ig, func(s, d string) error {
		if err := reflinkFile(s, d); err != nil {
			return copyFile(s, d)
		}
		return nil
	})
}

// copyBackend always works: a plain recursive copy.
type copyBackend struct{}

func (copyBackend) Name() string { return "copy" }
func (copyBackend) Clone(src, dst string, ig *Ignore) error {
	return cloneTree(src, dst, ig, copyFile)
}

// cloneTree walks src and recreates its structure under dst, delegating regular
// files to fileFn. Directories are created, symlinks are recreated as links,
// and ignored paths are skipped (whole subtrees for ignored dirs).
func cloneTree(src, dst string, ig *Ignore, fileFn func(s, d string) error) error {
	src = filepath.Clean(src)
	return filepath.WalkDir(src, func(p string, de fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(src, p)
		if err != nil {
			return err
		}
		if rel == "." {
			return os.MkdirAll(dst, 0o755)
		}
		if ig.Match(rel, de.IsDir()) {
			if de.IsDir() {
				return fs.SkipDir
			}
			return nil
		}
		target := filepath.Join(dst, rel)
		switch {
		case de.IsDir():
			info, err := de.Info()
			if err != nil {
				return err
			}
			return os.MkdirAll(target, info.Mode().Perm())
		case de.Type()&fs.ModeSymlink != 0:
			link, err := os.Readlink(p)
			if err != nil {
				return err
			}
			_ = os.Remove(target)
			return os.Symlink(link, target)
		case de.Type().IsRegular():
			return fileFn(p, target)
		default:
			// skip sockets, fifos, devices
			return nil
		}
	})
}

// Ignore decides which paths to skip during a clone.
type Ignore struct {
	names map[string]bool
}

// DefaultIgnoreNames are heavyweight, regenerable directories. .git is
// deliberately NOT here: the diff workflow depends on it.
var DefaultIgnoreNames = []string{
	"node_modules", "target", ".venv", "venv", "env", "__pycache__",
	".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build",
	"coverage", ".next", ".nuxt", ".turbo", ".gradle", ".tox", ".rifts",
}

// NewIgnore builds an Ignore from a list of directory/file base names.
// Pass nil for the default set; pass an empty slice to ignore nothing.
func NewIgnore(names []string) *Ignore {
	if names == nil {
		names = DefaultIgnoreNames
	}
	m := make(map[string]bool, len(names))
	for _, n := range names {
		m[n] = true
	}
	return &Ignore{names: m}
}

// Match reports whether rel should be skipped.
func (ig *Ignore) Match(rel string, isDir bool) bool {
	base := filepath.Base(rel)
	if ig.names[base] {
		return true
	}
	if !isDir && strings.HasSuffix(base, ".pyc") {
		return true
	}
	return false
}
