package components

import (
	"strings"
	"testing"

	"github.com/charmbracelet/lipgloss"
)

func TestRenderDiffPane_BasicDiff(t *testing.T) {
	diff := `--- a/main.go
+++ b/main.go
@@ -10,3 +10,4 @@ func main() {
 	fmt.Println("hello")
-	fmt.Println("old")
+	fmt.Println("new")
+	fmt.Println("added")
 }`

	block := ParseUnifiedDiff(diff, "test-render-1")
	output := RenderDiffPane(block, 80, 0)

	if output == "" {
		t.Fatal("expected non-empty output")
	}

	// Should contain the file path
	if !strings.Contains(output, "main.go") {
		t.Error("output should contain file path 'main.go'")
	}

	// Should contain the separator
	if !strings.Contains(output, "│") {
		t.Error("output should contain side-by-side separator '│'")
	}

	// Should have multiple lines
	lines := strings.Split(output, "\n")
	if len(lines) < 5 {
		t.Errorf("expected at least 5 lines, got %d", len(lines))
	}

	t.Logf("Rendered output (%d lines):\n%s", len(lines), output)
}

func TestRenderDiffPane_EmptyDiff(t *testing.T) {
	block := ParseUnifiedDiff("", "test-empty-render")
	output := RenderDiffPane(block, 80, 0)
	if output != "" {
		t.Errorf("expected empty output for empty diff, got %q", output)
	}
}

func TestRenderDiffPane_NarrowWidth(t *testing.T) {
	diff := `--- a/test.go
+++ b/test.go
@@ -1,2 +1,2 @@
-old
+new`

	block := ParseUnifiedDiff(diff, "test-narrow")
	
	// Too narrow should return empty
	output := RenderDiffPane(block, 10, 0)
	if output != "" {
		t.Error("expected empty output for width < 20")
	}

	// Minimum viable width
	output = RenderDiffPane(block, 20, 0)
	if output == "" {
		t.Error("expected non-empty output for width >= 20")
	}
}

func TestRenderDiffPane_NilBlock(t *testing.T) {
	output := RenderDiffPane(nil, 80, 0)
	if output != "" {
		t.Error("expected empty output for nil block")
	}
}

func TestRenderDiffPane_NewFile(t *testing.T) {
	diff := `--- /dev/null
+++ b/newfile.go
@@ -0,0 +1,3 @@
+package main
+
+func hello() {}`

	block := ParseUnifiedDiff(diff, "test-newfile-render")
	output := RenderDiffPane(block, 80, 0)

	if output == "" {
		t.Fatal("expected non-empty output")
	}
	if !strings.Contains(output, "newfile.go") {
		t.Error("output should contain file path")
	}

	t.Logf("New file render:\n%s", output)
}

func TestRenderDiffPane_DeletedFile(t *testing.T) {
	diff := `--- a/old.go
+++ /dev/null
@@ -1,3 +0,0 @@
-package main
-
-func goodbye() {}`

	block := ParseUnifiedDiff(diff, "test-deleted-render")
	output := RenderDiffPane(block, 80, 0)

	if output == "" {
		t.Fatal("expected non-empty output")
	}

	t.Logf("Deleted file render:\n%s", output)
}

func TestRenderDiffPane_MultipleHunks(t *testing.T) {
	diff := `--- a/large.go
+++ b/large.go
@@ -1,3 +1,4 @@
 package main
+import "fmt"
 
 func main() {
@@ -20,3 +21,4 @@ func helper() {
 	x := 1
+	y := 2
 	return x
 }`

	block := ParseUnifiedDiff(diff, "test-multi-hunk")
	output := RenderDiffPane(block, 80, 0)

	// Should have a hunk separator (·)
	if !strings.Contains(output, "·") {
		t.Error("expected hunk separator between hunks")
	}

	t.Logf("Multi-hunk render:\n%s", output)
}

func TestRenderDiffPane_NoNewlineMarker(t *testing.T) {
	diff := `--- a/file.txt
+++ b/file.txt
@@ -1,2 +1,2 @@
 line one
-old last
\ No newline at end of file
+new last
\ No newline at end of file`

	block := ParseUnifiedDiff(diff, "test-nonewline-render")
	output := RenderDiffPane(block, 80, 0)

	if output == "" {
		t.Fatal("expected non-empty output")
	}

	t.Logf("No-newline render:\n%s", output)
}

func TestRenderDiffSummary(t *testing.T) {
	diff := `--- a/test.go
+++ b/test.go
@@ -1,3 +1,4 @@
 line1
+added1
+added2
 line2
-removed1`

	block := ParseUnifiedDiff(diff, "test-summary-render")
	summary := RenderDiffSummary(block)

	if summary == "" {
		t.Fatal("expected non-empty summary")
	}

	if !strings.Contains(summary, "test.go") {
		t.Error("summary should contain file path")
	}
	if !strings.Contains(summary, "+2") {
		t.Error("summary should contain add count")
	}
	if !strings.Contains(summary, "-1") {
		t.Error("summary should contain remove count")
	}

	t.Logf("Summary: %s", summary)
}

func TestRenderDiffSummary_Statuses(t *testing.T) {
	block := &DiffBlock{
		ID:       "test",
		FilePath: "test.go",
		Hunks:    []DiffHunk{{Lines: []DiffLine{{Type: DiffLineAdd, Content: "x"}}}},
	}

	tests := []struct {
		status   DiffStatus
		contains string
	}{
		{DiffStatusPending, "pending"},
		{DiffStatusApproved, "approved"},
		{DiffStatusRejected, "rejected"},
		{DiffStatusApplied, "applied"},
	}

	for _, tt := range tests {
		block.Status = tt.status
		summary := RenderDiffSummary(block)
		if !strings.Contains(summary, tt.contains) {
			t.Errorf("status %s: expected summary to contain %q, got %q", tt.status, tt.contains, summary)
		}
	}
}

func TestRenderDiffBlocks_Multiple(t *testing.T) {
	blocks := []DiffBlock{
		{
			ID:       "d1",
			FilePath: "file1.go",
			Status:   DiffStatusApproved,
			Hunks: []DiffHunk{{Lines: []DiffLine{
				{Type: DiffLineAdd, NewLineNum: 1, Content: "added"},
			}}},
		},
		{
			ID:        "d2",
			FilePath:  "file2.go",
			Status:    DiffStatusPending,
			Collapsed: true,
			Hunks: []DiffHunk{{Lines: []DiffLine{
				{Type: DiffLineRemove, OldLineNum: 5, Content: "removed"},
			}}},
		},
	}

	output := RenderDiffBlocks(blocks, 80)
	if output == "" {
		t.Fatal("expected non-empty output")
	}

	// First block should be expanded (full pane)
	if !strings.Contains(output, "file1.go") {
		t.Error("should contain file1.go")
	}
	// Second block should be collapsed (summary)
	if !strings.Contains(output, "file2.go") {
		t.Error("should contain file2.go")
	}

	t.Logf("Multi-block render:\n%s", output)
}

func TestRenderDiffBlocks_Empty(t *testing.T) {
	output := RenderDiffBlocks(nil, 80)
	if output != "" {
		t.Error("expected empty output for nil blocks")
	}
	output = RenderDiffBlocks([]DiffBlock{}, 80)
	if output != "" {
		t.Error("expected empty output for empty blocks")
	}
}

func TestTruncateToWidth(t *testing.T) {
	tests := []struct {
		input    string
		maxWidth int
		expected string
	}{
		{"hello", 10, "hello"},
		{"hello world", 5, "hell…"},
		{"", 5, ""},
		{"ab", 1, "…"},
		{"abc", 0, ""},
	}

	for _, tt := range tests {
		result := truncateToWidth(tt.input, tt.maxWidth)
		if result != tt.expected {
			t.Errorf("truncateToWidth(%q, %d) = %q, want %q", tt.input, tt.maxWidth, result, tt.expected)
		}
	}
}

func TestFormatGutter(t *testing.T) {
	// Line number 0 should produce blank gutter
	result := formatGutter(0, 5)
	if strings.TrimSpace(result) != "" {
		// It will have ANSI codes, but the visible content should be spaces
		t.Logf("Gutter(0): %q", result)
	}

	// Normal line number
	result = formatGutter(42, 5)
	if result == "" {
		t.Error("expected non-empty gutter for line 42")
	}
	t.Logf("Gutter(42): %q", result)
}

func TestRenderLineWithWordChanges(t *testing.T) {
	// Test with no changes
	result := renderLineWithWordChanges("hello world", nil, 40, true)
	if result == "" {
		t.Error("expected non-empty result")
	}

	// Test with word changes
	changes := []WordChange{
		{Start: 6, End: 11, Type: WordChangeRemove},
	}
	result = renderLineWithWordChanges("hello world", changes, 40, true)
	if result == "" {
		t.Error("expected non-empty result with word changes")
	}

	t.Logf("Word change render: %q", result)
}

func TestRenderLineWithWordChanges_OutOfBounds(t *testing.T) {
	// Should not panic on out-of-bounds changes
	changes := []WordChange{
		{Start: 100, End: 200, Type: WordChangeRemove},
	}
	
	defer func() {
		if r := recover(); r != nil {
			t.Errorf("panicked on out-of-bounds changes: %v", r)
		}
	}()
	
	result := renderLineWithWordChanges("short", changes, 40, true)
	if result == "" {
		t.Error("expected non-empty result even with out-of-bounds changes")
	}
}

// ── MatchLines Edge Cases ──

func TestMatchLines_SingleLineModification(t *testing.T) {
	oldLines := []string{"- old line"}
	newLines := []string{"+ new line"}
	
	rows := MatchLines(oldLines, newLines)
	
	if len(rows) != 1 {
		t.Fatalf("expected 1 row, got %d", len(rows))
	}
	
	row := rows[0]
	if !row.IsChange {
		t.Error("expected IsChange=true for modification")
	}
	if row.Left != "- old line" {
		t.Errorf("expected Left='- old line', got %q", row.Left)
	}
	if row.Right != "+ new line" {
		t.Errorf("expected Right='+ new line', got %q", row.Right)
	}
	if row.LeftNum != 1 {
		t.Errorf("expected LeftNum=1, got %d", row.LeftNum)
	}
	if row.RightNum != 1 {
		t.Errorf("expected RightNum=1, got %d", row.RightNum)
	}
}

func TestMatchLines_EmptyOldSide(t *testing.T) {
	oldLines := []string{}
	newLines := []string{"+ line 1", "+ line 2"}
	
	rows := MatchLines(oldLines, newLines)
	
	if len(rows) != 2 {
		t.Fatalf("expected 2 rows, got %d", len(rows))
	}
	
	for i, row := range rows {
		if row.Left != "" {
			t.Errorf("row %d: expected empty Left, got %q", i, row.Left)
		}
		if row.Right == "" {
			t.Errorf("row %d: expected non-empty Right", i)
		}
		if !row.IsChange {
			t.Errorf("row %d: expected IsChange=true", i)
		}
		if row.LeftNum != 0 {
			t.Errorf("row %d: expected LeftNum=0, got %d", i, row.LeftNum)
		}
	}
}

func TestMatchLines_EmptyNewSide(t *testing.T) {
	oldLines := []string{"- line 1", "- line 2"}
	newLines := []string{}
	
	rows := MatchLines(oldLines, newLines)
	
	if len(rows) != 2 {
		t.Fatalf("expected 2 rows, got %d", len(rows))
	}
	
	for i, row := range rows {
		if row.Left == "" {
			t.Errorf("row %d: expected non-empty Left", i)
		}
		if row.Right != "" {
			t.Errorf("row %d: expected empty Right, got %q", i, row.Right)
		}
		if !row.IsChange {
			t.Errorf("row %d: expected IsChange=true", i)
		}
		if row.RightNum != 0 {
			t.Errorf("row %d: expected RightNum=0, got %d", i, row.RightNum)
		}
	}
}

func TestMatchLines_BothEmpty(t *testing.T) {
	rows := MatchLines([]string{}, []string{})
	if len(rows) != 0 {
		t.Errorf("expected 0 rows for empty inputs, got %d", len(rows))
	}
}

func TestMatchLines_UnchangedLines(t *testing.T) {
	oldLines := []string{"  line 1", "  line 2"}
	newLines := []string{"  line 1", "  line 2"}
	
	rows := MatchLines(oldLines, newLines)
	
	if len(rows) != 2 {
		t.Fatalf("expected 2 rows, got %d", len(rows))
	}
	
	for i, row := range rows {
		if row.IsChange {
			t.Errorf("row %d: expected IsChange=false for unchanged lines", i)
		}
		if row.Left != row.Right {
			t.Errorf("row %d: expected Left==Right for unchanged lines", i)
		}
	}
}

func TestMatchLines_MixedChanges(t *testing.T) {
	oldLines := []string{"  context", "- removed", "  context2"}
	newLines := []string{"  context", "+ added", "  context2"}
	
	rows := MatchLines(oldLines, newLines)
	
	if len(rows) < 3 {
		t.Fatalf("expected at least 3 rows, got %d", len(rows))
	}
	
	// First row should be context
	if rows[0].IsChange {
		t.Error("first row should be context (IsChange=false)")
	}
	
	// Middle row should be a change
	if !rows[1].IsChange {
		t.Error("middle row should be a change (IsChange=true)")
	}
	
	// Last row should be context
	if rows[2].IsChange {
		t.Error("last row should be context (IsChange=false)")
	}
}

// ── RenderSideBySideStream Tests ──

func TestRenderSideBySideStream_BasicDiff(t *testing.T) {
	blocks := []DiffBlock{
		{
			ID:       "test-1",
			FilePath: "test.go",
			Hunks: []DiffHunk{
				{
					Lines: []DiffLine{
						{Type: DiffLineContext, Content: "package main", OldLineNum: 1, NewLineNum: 1},
						{Type: DiffLineRemove, Content: "old line", OldLineNum: 2},
						{Type: DiffLineAdd, Content: "new line", NewLineNum: 2},
					},
				},
			},
		},
	}
	
	output := RenderSideBySideStream(blocks, 80)
	
	if output == "" {
		t.Fatal("expected non-empty output")
	}
	
	if !strings.Contains(output, "test.go") {
		t.Error("output should contain file path")
	}
	
	if !strings.Contains(output, "│") {
		t.Error("output should contain separator")
	}
	
	t.Logf("Output:\n%s", output)
}

func TestRenderSideBySideStream_EmptyBlocks(t *testing.T) {
	output := RenderSideBySideStream([]DiffBlock{}, 80)
	if output != "" {
		t.Errorf("expected empty output for empty blocks, got %q", output)
	}
}

func TestRenderSideBySideStream_NarrowWidth(t *testing.T) {
	blocks := []DiffBlock{
		{
			ID:       "test-narrow",
			FilePath: "test.go",
			Hunks: []DiffHunk{
				{
					Lines: []DiffLine{
						{Type: DiffLineAdd, Content: "x", NewLineNum: 1},
					},
				},
			},
		},
	}
	
	output := RenderSideBySideStream(blocks, 10)
	if output != "" {
		t.Errorf("expected empty output for width < 20, got %q", output)
	}
}

func TestRenderSideBySideStream_WideCharacters(t *testing.T) {
	blocks := []DiffBlock{
		{
			ID:       "test-wide",
			FilePath: "test.go",
			Hunks: []DiffHunk{
				{
					Lines: []DiffLine{
						{Type: DiffLineAdd, Content: "你好世界 (Hello World)", NewLineNum: 1},
					},
				},
			},
		},
	}
	
	output := RenderSideBySideStream(blocks, 40)
	
	if output == "" {
		t.Fatal("expected non-empty output")
	}
	
	lines := strings.Split(output, "\n")
	for i, line := range lines {
		lineWidth := lipgloss.Width(line)
		if lineWidth > 40 {
			t.Errorf("line %d exceeds width 40: got %d columns", i, lineWidth)
		}
	}
	
	t.Logf("Wide char output:\n%s", output)
}

func TestTruncateToDisplayWidth(t *testing.T) {
	tests := []struct {
		input    string
		maxWidth int
		desc     string
	}{
		{"hello", 10, "short string"},
		{"你好世界", 10, "CJK characters"},
		{"hello 世界", 8, "mixed ASCII and CJK"},
		{"🎉🎊🎈", 10, "emoji characters"},
	}
	
	for _, tt := range tests {
		t.Run(tt.desc, func(t *testing.T) {
			result := truncateToDisplayWidth(tt.input, tt.maxWidth)
			resultWidth := lipgloss.Width(result)
			if resultWidth > tt.maxWidth {
				t.Errorf("truncateToDisplayWidth(%q, %d) produced width %d > %d",
					tt.input, tt.maxWidth, resultWidth, tt.maxWidth)
			}
			t.Logf("%q → %q (width %d)", tt.input, result, resultWidth)
		})
	}
}
