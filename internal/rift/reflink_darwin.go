//go:build darwin

package rift

import (
	"syscall"
	"unsafe"
)

// sysClonefile is the macOS clonefile(2) syscall number (arm64 & x86_64).
const sysClonefile = 462

// reflinkFile creates dst as an APFS copy-on-write clone of src. dst must not
// already exist. clonefile preserves mode and attributes automatically.
func reflinkFile(src, dst string) error {
	sp, err := syscall.BytePtrFromString(src)
	if err != nil {
		return err
	}
	dp, err := syscall.BytePtrFromString(dst)
	if err != nil {
		return err
	}
	_, _, errno := syscall.Syscall(
		sysClonefile,
		uintptr(unsafe.Pointer(sp)),
		uintptr(unsafe.Pointer(dp)),
		0, // flags
	)
	if errno != 0 {
		return errno
	}
	return nil
}
