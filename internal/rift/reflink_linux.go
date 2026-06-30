//go:build linux

package rift

import (
	"os"
	"syscall"
)

// ficlone is the ioctl request that clones a whole file via reflink
// (FICLONE on Linux, supported by btrfs, XFS-with-reflink, and others).
const ficlone = 0x40049409

// reflinkFile creates dst as a copy-on-write clone of src. Returns a non-nil
// error if the filesystem does not support reflinks or src/dst differ in fs.
func reflinkFile(src, dst string) error {
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
	if _, _, errno := syscall.Syscall(syscall.SYS_IOCTL, d.Fd(), uintptr(ficlone), s.Fd()); errno != 0 {
		_ = os.Remove(dst)
		return errno
	}
	return nil
}
