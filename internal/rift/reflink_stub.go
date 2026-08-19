//go:build !linux && !darwin

package rift

import "errors"

// reflinkFile returns an error on platforms/filesystems without reflink
// support, so the copy backend is selected automatically. Windows lands here.
func reflinkFile(src, dst string) error {
	return errors.New("rift: reflink not supported on this platform")
}
