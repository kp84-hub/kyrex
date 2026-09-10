package tui

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/kp84-hub/kx/internal/rift"
)

func writeProviderConfig(t *testing.T, root, body string) {
	t.Helper()
	dir := filepath.Join(root, ".px")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "config.json"), []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestModelPickerUsesWorkspaceProviderConfig(t *testing.T) {
	home := t.TempDir()
	source := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("KYREX_PROVIDER", "")
	t.Setenv("KYREX_API_KEY", "")
	t.Setenv("KYREX_BASE_URL", "")
	t.Setenv("OPENAI_API_KEY", "")
	t.Setenv("OPENAI_BASE_URL", "")

	writeProviderConfig(t, home, "{\"provider\":\"openai\",\"api_key\":\"global-key\",\"base_url\":\"https://global.example/v1\"}")
	writeProviderConfig(t, source, "{\"provider\":\"Anthropic\",\"api_key\":\"workspace-key\",\"base_url\":\"https://workspace.example/v1\"}")

	m := NewModel(nil)
	m.Sidebar.CurrentProvider = "OpenAI"
	m.Workspace = &rift.Workspace{Source: source}

	if got := m.getProvider(); got != "anthropic" {
		t.Fatalf("provider = %q, want anthropic", got)
	}
	if got := m.getAPIKey(); got != "workspace-key" {
		t.Fatalf("api key = %q, want workspace key", got)
	}
	if got := m.getBaseURL(); got != "https://workspace.example/v1" {
		t.Fatalf("base URL = %q, want workspace endpoint", got)
	}
}

func TestModelPickerFallsBackToGlobalConfig(t *testing.T) {
	home := t.TempDir()
	source := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("KYREX_PROVIDER", "")
	t.Setenv("KYREX_API_KEY", "")
	t.Setenv("KYREX_BASE_URL", "")
	t.Setenv("OPENAI_API_KEY", "")
	t.Setenv("OPENAI_BASE_URL", "")

	writeProviderConfig(t, home, "{\"provider\":\"openai\",\"api_key\":\"global-key\",\"base_url\":\"https://global.example/v1\"}")
	m := NewModel(nil)
	m.Workspace = &rift.Workspace{Source: source}

	if got := m.getProvider(); got != "openai" {
		t.Fatalf("provider = %q, want openai", got)
	}
	if got := m.getAPIKey(); got != "global-key" {
		t.Fatalf("api key = %q, want global key", got)
	}
	if got := m.getBaseURL(); got != "https://global.example/v1" {
		t.Fatalf("base URL = %q, want global endpoint", got)
	}
}
