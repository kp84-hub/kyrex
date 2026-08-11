package tui

import (
	"strings"
	"testing"
)

func TestRenderSideBySide_LineNumbers(t *testing.T) {
	// Simulate a unified diff from Python's difflib.unified_diff()
	// File has 5 lines, we change line 3
	diff := `--- a/test.py
+++ b/test.py
@@ -1,5 +1,5 @@
 def foo():
-    print("old")
+    print("new")
     return 42
`

	got := renderSideBySide(diff, 40)
	lines := strings.Split(got, "\n")

	if len(lines) < 6 {
		t.Fatalf("expected at least 6 lines, got %d:\n%s", len(lines), got)
	}

	// Check: header lines preserved
	if !strings.Contains(lines[0], "---") {
		t.Errorf("line 0 should contain --- header, got: %s", lines[0])
	}
	if !strings.Contains(lines[1], "+++") {
		t.Errorf("line 1 should contain +++ header, got: %s", lines[1])
	}
	if !strings.Contains(lines[2], "@@") {
		t.Errorf("line 2 should contain @@ header, got: %s", lines[2])
	}

	// Check: context line (def foo():) — should show line 1 on both sides
	t.Logf("context line: %s", lines[3])
	if !strings.Contains(lines[3], "1 ") {
		t.Errorf("context line should show '1' in gutter, got: %s", lines[3])
	}

	// Check: changed line — left shows old line 2, right shows new line 2
	t.Logf("changed line (old): %s", lines[4])
	if !strings.Contains(lines[4], "2 ") {
		t.Errorf("changed line should show '2' in gutter, got: %s", lines[4])
	}

	// Check: next context line (return 42) — should show line 3 / 3
	t.Logf("context line: %s", lines[5])
	if !strings.Contains(lines[5], "3 ") {
		t.Errorf("second context line should show '3' in gutter, got: %s", lines[5])
	}
}

func TestRenderSideBySide_DeletionsAndAdditions(t *testing.T) {
	// Test standalone deletions and additions
	// Old has lines 1-4, new has lines 1-3 (line 2 removed, line 4 unchanged)
	diff := `--- a/old.py
+++ b/new.py
@@ -1,4 +1,3 @@
 line1
-line2
 line3
-line4
+line4_modified
`

	got := renderSideBySide(diff, 40)
	lines := strings.Split(got, "\n")

	if len(lines) < 7 {
		t.Fatalf("expected at least 7 lines, got %d:\n%s", len(lines), got)
	}

	// Print for inspection
	for i, l := range lines {
		t.Logf("line %d: %s", i, l)
	}

	// @@ header at line 2
	if !strings.Contains(lines[2], "@@") {
		t.Errorf("line 2 should be @@ header, got: %s", lines[2])
	}

	// line1 context — line 3
	if !strings.Contains(lines[3], "1") {
		t.Errorf("line1 context should show gutter '1', got: %s", lines[3])
	}

	// -line2 deletion — left shows 2, right is blank
	if !strings.Contains(lines[4], "2") {
		t.Errorf("deletion should show gutter '2' on left, got: %s", lines[4])
	}

	// line3 context — should show line 3 old / line 2 new
	// (oldLineNum=3, newLineNum=2 because deletion was skipped on new side)
	t.Logf("line3 context line: %s", lines[5])
}

func TestRenderSideBySide_AllLineTypesStayInSync(t *testing.T) {
	diff := `--- a/test.txt
+++ b/test.txt
@@ -1,4 +1,4 @@
 context
-old
+new
-deleted
 context-after-deletion
+added
`

	got := renderSideBySide(diff, 40)
	if got == "" {
		t.Fatal("renderSideBySide returned empty output")
	}

	// This input exercises both header branches, changed-line pairs, pure
	// deletion, pure addition, and context in one call. The three parallel
	// slices must therefore produce one result per entry.
	lines := strings.Split(got, "\n")
	if len(lines) != 8 {
		t.Fatalf("expected exactly 8 output lines, got %d:\n%s", len(lines), got)
	}

	// Pure deletion and pure addition must render an intentional, visible
	// placeholder on their empty side rather than plain whitespace.
	if !strings.Contains(lines[5], "·") {
		t.Errorf("pure deletion should contain visible empty-side placeholder, got: %q", lines[5])
	}
	if !strings.Contains(lines[7], "·") {
		t.Errorf("pure addition should contain visible empty-side placeholder, got: %q", lines[7])
	}
}
