package tui

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// makeSessionJSON creates a temporary .px_sessions/main.json file and returns
// its path. The caller is responsible for cleanup (removing the parent dir).
func makeSessionJSON(t *testing.T, history []map[string]interface{}) string {
	t.Helper()
	dir := t.TempDir()
	sessionDir := filepath.Join(dir, ".px_sessions")
	if err := os.MkdirAll(sessionDir, 0755); err != nil {
		t.Fatalf("mkdir session dir: %v", err)
	}
	data := map[string]interface{}{
		"history": history,
	}
	b, err := json.MarshalIndent(data, "", "  ")
	if err != nil {
		t.Fatalf("marshal session: %v", err)
	}
	path := filepath.Join(sessionDir, "main.json")
	if err := os.WriteFile(path, b, 0644); err != nil {
		t.Fatalf("write session: %v", err)
	}
	return path
}

func TestExtractLaneStory_ToolCalls(t *testing.T) {
	history := []map[string]interface{}{
		{
			"role":    "user",
			"content": "Fix the build error",
		},
		{
			"role":    "assistant",
			"content": "Let me look at the build file.",
			"tool_calls": []map[string]interface{}{
				{
					"name": "read_local_file",
					"arguments": map[string]interface{}{
						"path": "Makefile",
					},
				},
			},
		},
		{
			"role":    "tool",
			"content": "all:\n\tgo build ./...",
		},
		{
			"role":    "assistant",
			"content": "I see the issue. Let me fix it.",
			"tool_calls": []map[string]interface{}{
				{
					"name": "write_file",
					"arguments": map[string]interface{}{
						"path":    "Makefile",
						"content": "all:\n\tgo build -o kx ./cmd/kx",
					},
				},
			},
		},
		{
			"role":    "tool",
			"content": "File written successfully.",
		},
		{
			"role":    "assistant",
			"content": "Build should work now.",
		},
	}

	path := makeSessionJSON(t, history)
	story, err := extractLaneStory(path, 4096)
	if err != nil {
		t.Fatalf("extractLaneStory: %v", err)
	}

	// Should contain tool call lines.
	if !strings.Contains(story, "→ read_local_file: Makefile") {
		t.Errorf("expected tool call line for read_local_file, got:\n%s", story)
	}
	if !strings.Contains(story, "→ write_file: Makefile") {
		t.Errorf("expected tool call line for write_file, got:\n%s", story)
	}

	// Should contain assistant text trimmed to 150 chars.
	if !strings.Contains(story, "Let me look at the build file.") {
		t.Errorf("expected assistant text content, got:\n%s", story)
	}
	if !strings.Contains(story, "I see the issue. Let me fix it.") {
		t.Errorf("expected second assistant text, got:\n%s", story)
	}
	if !strings.Contains(story, "Build should work now.") {
		t.Errorf("expected final assistant text, got:\n%s", story)
	}

	// Should NOT contain tool result content (success case).
	if strings.Contains(story, "File written successfully") {
		t.Errorf("tool result should not appear in story (success case)")
	}

	// Should NOT contain user message content.
	if strings.Contains(story, "Fix the build error") {
		t.Errorf("user message should not appear in story")
	}
}

func TestExtractLaneStory_ToolFailure(t *testing.T) {
	history := []map[string]interface{}{
		{
			"role":    "user",
			"content": "read the file",
		},
		{
			"role":    "assistant",
			"content": "",
			"tool_calls": []map[string]interface{}{
				{
					"name": "read_local_file",
					"arguments": map[string]interface{}{
						"path": "nonexistent.txt",
					},
				},
			},
		},
		{
			"role":    "tool",
			"content": "Error: file not found: nonexistent.txt",
		},
	}

	path := makeSessionJSON(t, history)
	story, err := extractLaneStory(path, 4096)
	if err != nil {
		t.Fatalf("extractLaneStory: %v", err)
	}

	// Should note the failure.
	if !strings.Contains(story, "✗ read_local_file: Error: file not found") {
		t.Errorf("expected failure notation, got:\n%s", story)
	}
}

func TestExtractLaneStory_SystemSkip(t *testing.T) {
	history := []map[string]interface{}{
		{
			"role":    "system",
			"content": "You are a helpful assistant.",
		},
		{
			"role":    "user",
			"content": "hello",
		},
		{
			"role":    "assistant",
			"content": "Hi! How can I help?",
		},
	}

	path := makeSessionJSON(t, history)
	story, err := extractLaneStory(path, 4096)
	if err != nil {
		t.Fatalf("extractLaneStory: %v", err)
	}

	// Should NOT contain system message.
	if strings.Contains(story, "You are a helpful assistant") {
		t.Errorf("system message should be skipped")
	}

	// Should contain assistant response.
	if !strings.Contains(story, "Hi! How can I help?") {
		t.Errorf("expected assistant response in story")
	}
}

func TestExtractLaneStory_EmptyHistory(t *testing.T) {
	history := []map[string]interface{}{}
	path := makeSessionJSON(t, history)
	story, err := extractLaneStory(path, 4096)
	if err != nil {
		t.Fatalf("extractLaneStory: %v", err)
	}
	if story != "" {
		t.Errorf("expected empty story for empty history, got: %q", story)
	}
}

func TestExtractLaneStory_OverCap(t *testing.T) {
	// Build a session with many rounds to force truncation.
	history := []map[string]interface{}{}
	for i := 0; i < 10; i++ {
		history = append(history, map[string]interface{}{
			"role":    "user",
			"content": "step",
		})
		history = append(history, map[string]interface{}{
			"role":    "assistant",
			"content": "Step " + strings.Repeat("A", 200) + " done.",
			"tool_calls": []map[string]interface{}{
				{
					"name": "read_local_file",
					"arguments": map[string]interface{}{
						"path": "file.go",
					},
				},
			},
		})
		// Tool result (success — should not appear)
		history = append(history, map[string]interface{}{
			"role":    "tool",
			"content": "ok",
		})
	}
	history = append(history, map[string]interface{}{
		"role":    "assistant",
		"content": "Final step complete.",
	})

	path := makeSessionJSON(t, history)
	// Use a small cap to force truncation.
	story, err := extractLaneStory(path, 500)
	if err != nil {
		t.Fatalf("extractLaneStory: %v", err)
	}

	// Should contain the omission marker.
	if !strings.Contains(story, "omitted") {
		t.Errorf("expected omission marker in truncated story, got:\n%s", story)
	}

	// Should be within cap.
	if len(story) > 500 {
		t.Errorf("story length %d exceeds cap 500", len(story))
	}

	// Should contain earliest portion (first assistant).
	if !strings.Contains(story, "Step AAAA") {
		t.Errorf("expected earliest portion to be present")
	}

	// Should contain latest portion.
	if !strings.Contains(story, "Final step complete") {
		t.Errorf("expected latest portion to be present")
	}
}

func TestExtractLaneStory_NoToolCalls(t *testing.T) {
	history := []map[string]interface{}{
		{
			"role":    "user",
			"content": "What is 2+2?",
		},
		{
			"role":    "assistant",
			"content": "2+2 = 4",
		},
	}

	path := makeSessionJSON(t, history)
	story, err := extractLaneStory(path, 4096)
	if err != nil {
		t.Fatalf("extractLaneStory: %v", err)
	}

	if !strings.Contains(story, "2+2 = 4") {
		t.Errorf("expected assistant response, got:\n%s", story)
	}
}

func TestExtractLaneStory_MissingFile(t *testing.T) {
	_, err := extractLaneStory("/nonexistent/path/main.json", 4096)
	if err == nil {
		t.Errorf("expected error for missing file")
	}
}

func TestExtractLaneStory_TextTruncation(t *testing.T) {
	// Assistant text longer than 150 chars should be truncated.
	longText := strings.Repeat("word ", 50) // ~250 chars
	history := []map[string]interface{}{
		{
			"role":    "user",
			"content": "go",
		},
		{
			"role":    "assistant",
			"content": longText,
		},
	}

	path := makeSessionJSON(t, history)
	story, err := extractLaneStory(path, 4096)
	if err != nil {
		t.Fatalf("extractLaneStory: %v", err)
	}

	// Should be truncated with "..."
	if !strings.Contains(story, "...") || len(story) > 160 {
		t.Errorf("expected truncated text, length=%d, story:\n%s", len(story), story)
	}
}

func TestExtractLaneStory_DefaultCap(t *testing.T) {
	history := []map[string]interface{}{
		{
			"role":    "user",
			"content": "go",
		},
		{
			"role":    "assistant",
			"content": "ok",
		},
	}
	path := makeSessionJSON(t, history)
	// Zero maxBytes should default to 4096.
	story, err := extractLaneStory(path, 0)
	if err != nil {
		t.Fatalf("extractLaneStory: %v", err)
	}
	if !strings.Contains(story, "ok") {
		t.Errorf("expected story with default cap")
	}
}

func TestNormalizeSessionPath(t *testing.T) {
	path := normalizeSessionPath("/tmp/lane-0")
	expected := "/tmp/lane-0/.px_sessions/main.json"
	if path != expected {
		t.Errorf("normalizeSessionPath(%q) = %q, want %q", "/tmp/lane-0", path, expected)
	}
}
