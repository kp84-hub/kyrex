//go:build unix

package kyrex_engine

import (
	"os/exec"
	"syscall"
)

// configureDetached isolates the daemon from the TUI's process group so a
// closed terminal cannot HUP it. The daemon deliberately outlives the app.
func configureDetached(cmd *exec.Cmd) {
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.Setpgid = true
}
