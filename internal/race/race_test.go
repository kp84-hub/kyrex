package race

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestDefaultGateCommand_GoModPresent(t *testing.T) {
	dir := t.TempDir()

	// Create go.mod
	if err := os.WriteFile(filepath.Join(dir, "go.mod"), []byte("module test\n"), 0644); err != nil {
		t.Fatal(err)
	}

	cmd := DefaultGateCommand(dir)
	if cmd != "go build ./..." {
		t.Errorf("expected \"go build ./...\", got %q", cmd)
	}
}

func TestDefaultGateCommand_GoModAbsent(t *testing.T) {
	dir := t.TempDir()

	// No go.mod
	cmd := DefaultGateCommand(dir)
	if cmd != "true" {
		t.Errorf("expected \"true\", got %q", cmd)
	}
}

func TestDefaultGateCommand_NonexistentDir(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "nonexistent")

	cmd := DefaultGateCommand(dir)
	if cmd != "true" {
		t.Errorf("expected \"true\", got %q", cmd)
	}
}

func TestSetModelInConfig_UnparseableConfig(t *testing.T) {
	const model = "test-model"

	for name, content := range map[string]string{
		"empty":     "",
		"truncated": `{"model": "old-mod`,
		"invalid":   `not json {`,
	} {
		t.Run(name, func(t *testing.T) {
			dir := t.TempDir()
			pxDir := filepath.Join(dir, ".px")
			configPath := filepath.Join(pxDir, "config.json")
			if err := os.MkdirAll(pxDir, 0755); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(configPath, []byte(content), 0644); err != nil {
				t.Fatal(err)
			}

			if err := setModelInConfig(dir, model); err != nil {
				t.Fatalf("setModelInConfig with unparseable config returned error: %v", err)
			}

			data, err := os.ReadFile(configPath)
			if err != nil {
				t.Fatal(err)
			}
			var cfg map[string]any
			if err := json.Unmarshal(data, &cfg); err != nil {
				t.Fatalf("rewritten config is not valid JSON: %v", err)
			}
			if got, ok := cfg["model"].(string); !ok || got != model {
				t.Fatalf("expected model %q, got %#v", model, cfg["model"])
			}
		})
	}
}

// TestSetEnvValueReplacesDuplicates guards the consult/race multi-model bug:
// lane subprocesses inherit the parent env, which may already contain
// KYREX_MODEL (the main session model). Spawn must REPLACE that value per
// lane, never append — duplicate keys are ambiguous and the process may end
// up running the parent's model for every lane.
func TestSetEnvValueReplacesDuplicates(t *testing.T) {
	env := []string{
		"PATH=/usr/bin:/bin",
		"KYREX_MODEL=deepseek/deepseek-v4-flash-0731",
		"HOME=/home/kplane",
		"KYREX_API_KEY=sk-test",
	}

	got := setEnvValue(env, "KYREX_MODEL", "kimi-k2.7-code")

	// Exactly one KYREX_MODEL entry, and it is the new value.
	count := 0
	for _, entry := range got {
		if entry == "KYREX_MODEL=kimi-k2.7-code" {
			count++
		}
		if entry == "KYREX_MODEL=deepseek/deepseek-v4-flash-0731" {
			t.Errorf("stale KYREX_MODEL survived: entry %q", entry)
		}
	}
	if count != 1 {
		t.Errorf("expected exactly 1 KYREX_MODEL=kimi-k2.7-code, got %d", count)
	}

	// Unrelated entries are preserved.
	foundAPIKey := false
	for _, entry := range got {
		if entry == "KYREX_API_KEY=sk-test" {
			foundAPIKey = true
		}
	}
	if !foundAPIKey {
		t.Errorf("unrelated env entries were dropped")
	}
}

// TestSetEnvValueAddsWhenAbsent verifies the fix works even when the
// key was never present: the value is added exactly once, at the end.
func TestSetEnvValueAddsWhenAbsent(t *testing.T) {
	env := []string{"PATH=/usr/bin", "HOME=/home/user"}
	got := setEnvValue(env, "KYREX_MODEL", "glm-5.2")

	count := 0
	for _, entry := range got {
		if entry == "KYREX_MODEL=glm-5.2" {
			count++
		}
	}
	if count != 1 {
		t.Errorf("expected exactly 1 KYREX_MODEL=glm-5.2, got %d (env=%q)", count, got)
	}
	if len(got) != 3 {
		t.Errorf("expected 3 entries, got %d (env=%q)", len(got), got)
	}
}

// TestSpawnOverridesInheritedKYREXModel is the end-to-end regression guard
// for the per-lane model bug: the parent process (main session, shell
// profile, or CI) often exports KYREX_MODEL for its own model. Every lane's
// engine subprocess must see that lane's model via KYREX_MODEL — the stale
// inherited value must be replaced by Spawn, not merely shadowed.
func TestSpawnOverridesInheritedKYREXModel(t *testing.T) {
	t.Setenv("KYREX_MODEL", "deepseek/deepseek-v4-flash-0731")

	const laneModel = "kimi-k2.7-code"
	dir := t.TempDir()
	l := &Lane{ID: 2, Model: laneModel, Dir: dir}

	if err := l.Spawn([]string{"sleep", "0"}, t.TempDir()); err != nil {
		t.Fatalf("Spawn returned error: %v", err)
	}
	if l.cmd == nil || l.cmd.Env == nil {
		t.Fatal("Spawn did not initialize cmd.Env")
	}

	found := false
	for _, e := range l.cmd.Env {
		switch {
		case e == "KYREX_MODEL="+laneModel:
			if found {
				t.Errorf("duplicate KYREX_MODEL entry in lane env")
			}
			found = true
		case strings.HasPrefix(e, "KYREX_MODEL="):
			t.Errorf("stale inherited KYREX_MODEL survived in lane env: %q", e)
		}
	}
	if !found {
		t.Errorf("KYREX_MODEL=%s missing from lane env", laneModel)
	}
}
