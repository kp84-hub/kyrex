package tui

import (
	"strings"
	"testing"

	"github.com/kp84-hub/kx/tui/components"
)

func TestCommandFailuresNeverRenderAsSuccess(t *testing.T) {
	cases := []struct {
		name   string
		result map[string]interface{}
	}{
		{"new engine authentication", map[string]interface{}{"status": "error", "returncode": float64(128), "error": "Git remote authentication failed"}},
		{"older engine authentication", map[string]interface{}{"status": "ok", "returncode": float64(128)}},
		{"failed build", map[string]interface{}{"status": "ok", "returncode": float64(1)}},
		{"integer exit", map[string]interface{}{"returncode": 42}},
		{"signal exit", map[string]interface{}{"returncode": float64(-15)}},
		{"status without error", map[string]interface{}{"status": "failed"}},
		{"error status without message", map[string]interface{}{"status": "error"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			m := applyEngineFrames(newConvModel(),
				MsgFromEngine{Type: "tool_start", ID: "fetch", Name: "run_command", Args: map[string]interface{}{"command": "git fetch origin"}},
				MsgFromEngine{Type: "tool_result", ID: "fetch", Name: "run_command", Result: tc.result},
			)
			if !strings.HasPrefix(m.ToolResult, "ERR:") {
				t.Fatalf("failed command displayed %q", m.ToolResult)
			}
			if last := m.Tools.Latest(); last == nil || last.State != ToolStateFailed {
				t.Fatalf("failed command telemetry: %#v", last)
			}
			for _, event := range m.Timeline.Events {
				if event.ID == "fetch" && event.Status != components.StatusFailed {
					t.Fatalf("failed command timeline status: %v", event.Status)
				}
			}
		})
	}
}

func TestSuccessfulCommandStillRendersAsSuccess(t *testing.T) {
	m := applyEngineFrames(newConvModel(),
		MsgFromEngine{Type: "tool_start", ID: "status", Name: "run_command"},
		MsgFromEngine{Type: "tool_result", ID: "status", Result: map[string]interface{}{"status": "ok", "returncode": float64(0)}},
	)
	if m.ToolResult != "OK" || m.Tools.Latest().State != ToolStateSuccess {
		t.Fatalf("successful command was marked failed: %q", m.ToolResult)
	}
}
