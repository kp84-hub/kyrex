//go:build !linux && !darwin

package rift

import "errors"

// ErrUnsupported indicates the platform/filesystem has no reflink support, so
// the copy backend will be selected automatically. Windows lands here.
var ErrUnsupported = errors.New("rift: reflink not supported on this platform")

func reflinkFile(src, dst string) error { return ErrUnsupported }
