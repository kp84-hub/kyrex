//go:build windows

package kyrex_engine

import (
	"os/exec"
	"syscall"
)

// configureDetached detaches the daemon from the TUI's console and process
// group on Windows so closing the terminal cannot kill it. The daemon
// deliberately outlives the app.
func configureDetached(cmd *exec.Cmd) {
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.CreationFlags = syscall.DETACHED_PROCESS | syscall.CREATE_NEW_PROCESS_GROUP
	cmd.SysProcAttr.HideWindow = true
}
