package tui

import (
	"strings"
	"testing"
)

func TestOverviewRendersFullyAfterCompletion(t *testing.T) {
	m := NewModel(nil)

	// Simulate: long CurrToken gets moved to history on completion
	fullNarration := `## Step 1: Initialize
Created project structure
Installed dependencies

## Step 2: Implement core
Wrote main logic for the engine
Added error handling
Refactored helper functions

## Step 3: Testing
Ran unit tests
Fixed edge cases
All tests passing

## Step 4: Finalize
Cleaned up code
Added documentation
Task complete.`

	m.CurrToken = fullNarration

	// Simulate completion: move CurrToken to history as _Overview:_
	m.History = append(m.History, "> create two files")
	m.History = append(m.History, "_Overview:_\n"+m.CurrToken)
	m.CurrToken = "" // cleared after move

	// Now CurrentTurnContent should be empty (no reasoning, no CurrToken)
	turnContent := m.CurrentTurnContent(80, 0)
	if turnContent != "" {
		t.Fatalf("Expected empty CurrentTurnContent after completion, got: %q", turnContent)
	}

	// HistoryContent should render the full overview
	historyContent, _ := m.HistoryContent(80)
	if !strings.Contains(historyContent, "Step 1: Initialize") {
		t.Fatal("History missing 'Step 1: Initialize' — Overview not fully rendered")
	}
	if !strings.Contains(historyContent, "Task complete.") {
		t.Fatal("History missing 'Task complete.' — Overview truncated after completion")
	}
	if !strings.Contains(historyContent, "Step 4: Finalize") {
		t.Fatal("History missing 'Step 4: Finalize' — Overview not fully rendered")
	}

	// FullViewportContent should also contain the full narration
	fullContent := m.FullViewportContent(80)
	if !strings.Contains(fullContent, "Step 1: Initialize") {
		t.Fatal("FullViewportContent missing 'Step 1: Initialize'")
	}
	if !strings.Contains(fullContent, "Task complete.") {
		t.Fatal("FullViewportContent missing 'Task complete.' — final summary is blank!")
	}
}

func TestOverviewNotBlankAfterCompletion(t *testing.T) {
	m := NewModel(nil)

	// Minimum viable completion scenario
	m.CurrToken = "Created files and verified output."

	m.History = append(m.History, "> test task")
	m.History = append(m.History, "_Overview:_\n"+m.CurrToken)
	m.CurrToken = ""

	fullContent := m.FullViewportContent(80)
	if strings.TrimSpace(fullContent) == "" {
		t.Fatal("FullViewportContent is BLANK after completion — final overview is missing!")
	}
	if !strings.Contains(fullContent, "Created files and verified output.") {
		t.Fatal("Overview content missing from FullViewportContent after completion")
	}
}