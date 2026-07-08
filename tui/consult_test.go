package tui

import (
	"strings"
	"testing"
)

func TestExtractContext_Basic(t *testing.T) {
	history := []string{
		"> /model gpt-4",
		"_Overview:_\nModel set to gpt-4.",
		"> what is the capital of France?",
		"_Thinking:_\nThe user asked about France's capital.",
		"_Overview:_\nThe capital of France is Paris.",
		"> translate hello to french",
		"_Logs:_\nread_local_file dictionary.txt",
		"_DiffContent:_\n--- a/file\n+++ b/file\n@@ -1 +1 @@\n-hello\n+bonjour",
		"_Overview:_\n'hello' translates to 'bonjour'.",
	}

	result := extractContext(history, 10*1024) // generous cap

	// Should include user messages and overviews
	if !strings.Contains(result, "what is the capital of France?") {
		t.Errorf("expected user message in context, got:\n%s", result)
	}
	if !strings.Contains(result, "capital of France is Paris") {
		t.Errorf("expected assistant response in context, got:\n%s", result)
	}
	if !strings.Contains(result, "translate hello to french") {
		t.Errorf("expected user message in context, got:\n%s", result)
	}

	// Should exclude _Logs:_ entries
	if strings.Contains(result, "_Logs:_") {
		t.Errorf("expected no _Logs:_ entries, got:\n%s", result)
	}
	// Should exclude _DiffContent:_ entries
	if strings.Contains(result, "_DiffContent:_") {
		t.Errorf("expected no _DiffContent:_ entries, got:\n%s", result)
	}
	// Should exclude _Thinking:_ entries
	if strings.Contains(result, "_Thinking:_") {
		t.Errorf("expected no _Thinking:_ entries, got:\n%s", result)
	}
}

func TestExtractContext_Ordering(t *testing.T) {
	history := []string{
		"> first message",
		"_Overview:_\nFirst response.",
		"> second message",
		"_Overview:_\nSecond response.",
		"> third message",
		"_Overview:_\nThird response.",
	}

	result := extractContext(history, 10*1024)

	// Should maintain chronological order
	first := strings.Index(result, "first message")
	second := strings.Index(result, "second message")
	third := strings.Index(result, "third message")

	if first < 0 || second < 0 || third < 0 {
		t.Fatal("expected all messages in result")
	}
	if !(first < second && second < third) {
		t.Errorf("expected chronological order, got first=%d second=%d third=%d", first, second, third)
	}
}

func TestExtractContext_Capping(t *testing.T) {
	// Create history entries that will exceed the cap
	history := []string{
		"> short msg",
		"_Overview:_\n" + strings.Repeat("A", 3000),
		"> another short msg",
		"_Overview:_\n" + strings.Repeat("B", 3000),
		"> third short msg",
		"_Overview:_\n" + strings.Repeat("C", 3000),
	}

	// Cap at 4KB — should include most recent entries
	result := extractContext(history, 4096)

	// The total should be <= cap
	if len(result) > 4096 {
		t.Errorf("result length %d exceeds cap 4096", len(result))
	}

	// Most recent entries should be present (they're prioritized)
	// History reverse order: third, second, first
	// Each user+overview pair is ~3KB. With 4KB cap, we should get at least one full pair.
	if !strings.Contains(result, "third short msg") {
		t.Errorf("expected most recent message in capped result")
	}

	// The oldest entry may be truncated
	// This is fine — just verify the cap is respected
}

func TestExtractContext_EmptyHistory(t *testing.T) {
	result := extractContext(nil, 1024)
	if result != "" {
		t.Errorf("expected empty string for nil history, got %q", result)
	}

	result = extractContext([]string{}, 1024)
	if result != "" {
		t.Errorf("expected empty string for empty history, got %q", result)
	}
}

func TestExtractContext_ZeroCap(t *testing.T) {
	history := []string{"> hello", "_Overview:_\nHi!"}
	result := extractContext(history, 0)
	// Zero cap should default to 6KB
	if result == "" {
		t.Errorf("expected nonzero result with zero cap (defaults to 6KB)")
	}
}

func TestExtractContext_OnlyNoise(t *testing.T) {
	history := []string{
		"_Logs:_\nread file x",
		"_DiffContent:_\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new",
		"_Thinking:_\nsome thinking",
	}
	result := extractContext(history, 1024)
	if result != "" {
		t.Errorf("expected empty result for only noise entries, got %q", result)
	}
}
