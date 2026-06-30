package rift

import (
	"io"
	"os"
	"path/filepath"
)

// copyFile is the universal fallback: a plain byte copy preserving mode.
func copyFile(src, dst string) error {
	s, err := os.Open(src)
	if err != nil {
		return err
	}
	defer s.Close()
	fi, err := s.Stat()
	if err != nil {
		return err
	}
	d, err := os.OpenFile(dst, os.O_RDWR|os.O_CREATE|os.O_TRUNC, fi.Mode().Perm())
	if err != nil {
		return err
	}
	defer d.Close()
	if _, err := io.Copy(d, s); err != nil {
		return err
	}
	return d.Close()
}

// probeReflink reports whether reflinks work in dir by attempting one. It
// writes two tiny temp files and cleans them up regardless of outcome.
func probeReflink(dir string) error {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	src := filepath.Join(dir, ".rift-probe-src")
	dst := filepath.Join(dir, ".rift-probe-dst")
	_ = os.Remove(src)
	_ = os.Remove(dst)
	if err := os.WriteFile(src, []byte("rift"), 0o600); err != nil {
		return err
	}
	defer os.Remove(src)
	err := reflinkFile(src, dst)
	_ = os.Remove(dst)
	return err
}
