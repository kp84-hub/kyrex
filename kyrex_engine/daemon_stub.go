//go:build !unix && !windows

package kyrex_engine

import "os/exec"

// configureDetached is a no-op on platforms without process-group controls.
func configureDetached(_ *exec.Cmd) {}
