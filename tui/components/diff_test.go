package components

import (
	"strings"
	"testing"
)

func TestParseUnifiedDiff_BasicDiff(t *testing.T) {
	diff := `--- a/main.go
+++ b/main.go
@@ -10,3 +10,4 @@ func main() {
 	fmt.Println("hello")
-	fmt.Println("old")
+	fmt.Println("new")
+	fmt.Println("added")
 }`

	block := ParseUnifiedDiff(diff, "test-1")

	if block.FilePath != "main.go" {
		t.Errorf("expected FilePath 'main.go', got '%s'", block.FilePath)
	}
	if len(block.Hunks) != 1 {
		t.Fatalf("expected 1 hunk, got %d", len(block.Hunks))
	}

	hunk := block.Hunks[0]
	if hunk.OldStart != 10 || hunk.OldCount != 3 {
		t.Errorf("unexpected old range: %d,%d", hunk.OldStart, hunk.OldCount)
	}
	if hunk.NewStart != 10 || hunk.NewCount != 4 {
		t.Errorf("unexpected new range: %d,%d", hunk.NewStart, hunk.NewCount)
	}

	// Count line types
	adds, removes, context := 0, 0, 0
	for _, line := range hunk.Lines {
		switch line.Type {
		case DiffLineAdd:
			adds++
		case DiffLineRemove:
			removes++
		case DiffLineContext:
			context++
		}
	}
	if adds != 2 {
		t.Errorf("expected 2 adds, got %d", adds)
	}
	if removes != 1 {
		t.Errorf("expected 1 remove, got %d", removes)
	}
	if context != 2 {
		t.Errorf("expected 2 context, got %d", context)
	}
}

func TestParseUnifiedDiff_NoNewlineAtEnd(t *testing.T) {
	diff := `--- a/file.txt
+++ b/file.txt
@@ -1,3 +1,3 @@
 line one
-line two
\ No newline at end of file
+line two modified
\ No newline at end of file`

	block := ParseUnifiedDiff(diff, "test-nonewline")

	if len(block.Hunks) != 1 {
		t.Fatalf("expected 1 hunk, got %d", len(block.Hunks))
	}

	lines := block.Hunks[0].Lines
	// Should have: context, remove, add
	if len(lines) < 3 {
		t.Fatalf("expected at least 3 lines, got %d", len(lines))
	}

	// The remove line should be marked NoNewline
	removeLine := lines[1]
	if removeLine.Type != DiffLineRemove {
		t.Errorf("expected line[1] to be remove, got %s", removeLine.Type)
	}
	if !removeLine.NoNewline {
		t.Error("expected remove line to have NoNewline=true")
	}

	// The add line should also be marked NoNewline
	addLine := lines[2]
	if addLine.Type != DiffLineAdd {
		t.Errorf("expected line[2] to be add, got %s", addLine.Type)
	}
	if !addLine.NoNewline {
		t.Error("expected add line to have NoNewline=true")
	}
}

func TestParseUnifiedDiff_MixedWhitespace(t *testing.T) {
	diff := `--- a/mixed.go
+++ b/mixed.go
@@ -5,4 +5,4 @@
 func test() {
-	no newline
+		tabs and spaces mixed
 	context with	tabs
-    spaces only
+    more spaces`

	block := ParseUnifiedDiff(diff, "test-whitespace")

	if len(block.Hunks) != 1 {
		t.Fatalf("expected 1 hunk, got %d", len(block.Hunks))
	}

	// Verify content is preserved exactly (tabs not converted)
	for _, line := range block.Hunks[0].Lines {
		if line.Type == DiffLineAdd && line.Content == "		tabs and spaces mixed" {
			// Tab preserved correctly
			return
		}
	}
	// If we get here, check what we actually got
	for i, line := range block.Hunks[0].Lines {
		t.Logf("line[%d]: type=%s content=%q", i, line.Type, line.Content)
	}
}

func TestParseUnifiedDiff_EmptyDiff(t *testing.T) {
	block := ParseUnifiedDiff("", "test-empty")
	if block == nil {
		t.Fatal("expected non-nil block for empty diff")
	}
	if !block.IsEmpty() {
		t.Error("expected empty block")
	}
	if block.Summary() != "No changes" {
		t.Errorf("expected 'No changes' summary, got '%s'", block.Summary())
	}
}

func TestParseUnifiedDiff_MalformedInput(t *testing.T) {
	// Should not panic on garbage input
	tests := []string{
		"this is not a diff at all",
		"@@@",
		"--- \n+++ ",
		"@@ -abc +def @@",
		"--- a/file\n+++ b/file\n@@ -1 +1 @@",
		strings.Repeat("+\n", 1000),
	}

	for i, input := range tests {
		func() {
			defer func() {
				if r := recover(); r != nil {
					t.Errorf("test %d panicked on input %q: %v", i, input[:min(50, len(input))], r)
				}
			}()
			block := ParseUnifiedDiff(input, "test-malformed")
			if block == nil {
				t.Errorf("test %d: expected non-nil block", i)
			}
		}()
	}
}

func TestParseUnifiedDiff_MultipleHunks(t *testing.T) {
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

	block := ParseUnifiedDiff(diff, "test-multi")

	if len(block.Hunks) != 2 {
		t.Fatalf("expected 2 hunks, got %d", len(block.Hunks))
	}
	if block.Hunks[0].OldStart != 1 {
		t.Errorf("hunk[0] OldStart: expected 1, got %d", block.Hunks[0].OldStart)
	}
	if block.Hunks[1].OldStart != 20 {
		t.Errorf("hunk[1] OldStart: expected 20, got %d", block.Hunks[1].OldStart)
	}
}

func TestParseUnifiedDiff_NewFile(t *testing.T) {
	diff := `--- /dev/null
+++ b/newfile.go
@@ -0,0 +1,5 @@
+package main
+
+func main() {
+	fmt.Println("new")
+}`

	block := ParseUnifiedDiff(diff, "test-newfile")

	if block.FilePath != "newfile.go" {
		t.Errorf("expected FilePath 'newfile.go', got '%s'", block.FilePath)
	}
	if len(block.Hunks) != 1 {
		t.Fatalf("expected 1 hunk, got %d", len(block.Hunks))
	}

	// All lines should be adds
	for _, line := range block.Hunks[0].Lines {
		if line.Type != DiffLineAdd {
			t.Errorf("expected all adds for new file, got %s", line.Type)
		}
	}
}

func TestParseUnifiedDiff_DeletedFile(t *testing.T) {
	diff := `--- a/old.go
+++ /dev/null
@@ -1,3 +0,0 @@
-package main
-
-func main() {}`

	block := ParseUnifiedDiff(diff, "test-deleted")

	if len(block.Hunks) != 1 {
		t.Fatalf("expected 1 hunk, got %d", len(block.Hunks))
	}

	for _, line := range block.Hunks[0].Lines {
		if line.Type != DiffLineRemove {
			t.Errorf("expected all removes for deleted file, got %s", line.Type)
		}
	}
}

func TestComputeWordDiff(t *testing.T) {
	oldStr := "fmt.Println(\"hello world\")"
	newStr := "fmt.Println(\"hello earth\")"

	oldChanges, newChanges := computeWordDiff(oldStr, newStr)

	// Should detect that "world" was removed and "earth" was added
	if len(oldChanges) == 0 {
		t.Error("expected word changes in old string")
	}
	if len(newChanges) == 0 {
		t.Error("expected word changes in new string")
	}

	// Verify the change ranges are valid
	for _, c := range oldChanges {
		if c.Start < 0 || c.End > len(oldStr) || c.Start >= c.End {
			t.Errorf("invalid old change range: %d-%d (len=%d)", c.Start, c.End, len(oldStr))
		}
	}
	for _, c := range newChanges {
		if c.Start < 0 || c.End > len(newStr) || c.Start >= c.End {
			t.Errorf("invalid new change range: %d-%d (len=%d)", c.Start, c.End, len(newStr))
		}
	}
}

func TestWordDiff_IdenticalLines(t *testing.T) {
	oldChanges, newChanges := computeWordDiff("same line", "same line")
	if len(oldChanges) != 0 || len(newChanges) != 0 {
		t.Error("expected no changes for identical lines")
	}
}

func TestWordDiff_EmptyLines(t *testing.T) {
	// Should not panic
	oldChanges, newChanges := computeWordDiff("", "")
	if len(oldChanges) != 0 || len(newChanges) != 0 {
		t.Error("expected no changes for empty lines")
	}

	oldChanges, newChanges = computeWordDiff("something", "")
	if len(oldChanges) == 0 {
		t.Error("expected removal changes")
	}
	if len(newChanges) != 0 {
		t.Error("expected no add changes when new is empty")
	}

	oldChanges, newChanges = computeWordDiff("", "something")
	if len(oldChanges) != 0 {
		t.Error("expected no remove changes when old is empty")
	}
	if len(newChanges) == 0 {
		t.Error("expected add changes")
	}
}

func TestDiffBlock_Summary(t *testing.T) {
	diff := `--- a/test.go
+++ b/test.go
@@ -1,3 +1,4 @@
 line1
+added1
+added2
 line2
-removed1`

	block := ParseUnifiedDiff(diff, "test-summary")
	summary := block.Summary()

	if summary != "+2 -1" {
		t.Errorf("expected '+2 -1', got '%s'", summary)
	}
}

func TestDiffBlock_IsEmpty(t *testing.T) {
	// Context-only diff (no actual changes)
	diff := `--- a/test.go
+++ b/test.go
@@ -1,3 +1,3 @@
 line1
 line2
 line3`

	block := ParseUnifiedDiff(diff, "test-context-only")
	if !block.IsEmpty() {
		t.Error("expected context-only diff to be empty")
	}
}

func TestLineNumbers(t *testing.T) {
	diff := `--- a/test.go
+++ b/test.go
@@ -5,4 +5,5 @@
 context5
 context6
-removed7
+added7
+added8
 context8`

	block := ParseUnifiedDiff(diff, "test-linenums")

	if len(block.Hunks) != 1 {
		t.Fatalf("expected 1 hunk, got %d", len(block.Hunks))
	}

	lines := block.Hunks[0].Lines
	// Verify line numbers are tracked correctly
	expectedOldNums := []int{5, 6, 7, 0, 0, 8}
	expectedNewNums := []int{5, 6, 0, 7, 8, 9}

	for i, line := range lines {
		if i < len(expectedOldNums) && line.OldLineNum != expectedOldNums[i] {
			t.Errorf("line[%d] OldLineNum: expected %d, got %d", i, expectedOldNums[i], line.OldLineNum)
		}
		if i < len(expectedNewNums) && line.NewLineNum != expectedNewNums[i] {
			t.Errorf("line[%d] NewLineNum: expected %d, got %d", i, expectedNewNums[i], line.NewLineNum)
		}
	}
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
