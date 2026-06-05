package components

import (
	"testing"
)

func TestDiffBlock_StreamingLifecycle(t *testing.T) {
	// Simulate streaming diff lifecycle
	diffID := "test-stream-1"
	
	// Initial streaming state (partial diff)
	partialDiff := `--- a/test.go
+++ b/test.go
@@ -1,3 +1,4 @@
 package main
+import "fmt"
 
 func main() {`

	block := ParseUnifiedDiff(partialDiff, diffID)
	if block == nil {
		t.Fatal("Expected non-nil diff block")
	}
	if block.ID != diffID {
		t.Errorf("Expected ID %s, got %s", diffID, block.ID)
	}
	if block.FilePath != "test.go" {
		t.Errorf("Expected FilePath test.go, got %s", block.FilePath)
	}
	if len(block.Hunks) != 1 {
		t.Errorf("Expected 1 hunk, got %d", len(block.Hunks))
	}

	// Complete streaming state (full diff)
	completeDiff := `--- a/test.go
+++ b/test.go
@@ -1,3 +1,4 @@
 package main
+import "fmt"
 
 func main() {
+	fmt.Println("Hello")
 }`

	block = ParseUnifiedDiff(completeDiff, diffID)
	if block == nil {
		t.Fatal("Expected non-nil diff block")
	}
	if len(block.Hunks) != 1 {
		t.Errorf("Expected 1 hunk, got %d", len(block.Hunks))
	}
	// Should have 2 additions now
	addCount := 0
	for _, hunk := range block.Hunks {
		for _, line := range hunk.Lines {
			if line.Type == DiffLineAdd {
				addCount++
			}
		}
	}
	if addCount != 2 {
		t.Errorf("Expected 2 additions, got %d", addCount)
	}
}

func TestDiffBlock_EmptyStreamingUpdate(t *testing.T) {
	// Test that empty diff doesn't crash
	block := ParseUnifiedDiff("", "empty-stream")
	if block == nil {
		t.Fatal("Expected non-nil diff block even for empty diff")
	}
	if block.ID != "empty-stream" {
		t.Errorf("Expected ID empty-stream, got %s", block.ID)
	}
	if len(block.Hunks) != 0 {
		t.Errorf("Expected 0 hunks for empty diff, got %d", len(block.Hunks))
	}
}

func TestDiffBlock_MalformedStreamingUpdate(t *testing.T) {
	// Test that malformed diff doesn't crash
	malformedDiff := `this is not a valid diff
just some random text
--- incomplete header`

	block := ParseUnifiedDiff(malformedDiff, "malformed-stream")
	if block == nil {
		t.Fatal("Expected non-nil diff block even for malformed diff")
	}
	// Should parse what it can without crashing
	if block.ID != "malformed-stream" {
		t.Errorf("Expected ID malformed-stream, got %s", block.ID)
	}
}

func TestDiffBlock_IncrementalHunks(t *testing.T) {
	// Test adding hunks incrementally (simulating streaming)
	diffID := "incremental-test"
	
	// First chunk: one hunk
	chunk1 := `--- a/large.go
+++ b/large.go
@@ -1,3 +1,4 @@
 package main
+import "fmt"
 
 func main() {`

	block1 := ParseUnifiedDiff(chunk1, diffID)
	if len(block1.Hunks) != 1 {
		t.Errorf("Expected 1 hunk in chunk1, got %d", len(block1.Hunks))
	}

	// Second chunk: two hunks (simulating more content arriving)
	chunk2 := `--- a/large.go
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

	block2 := ParseUnifiedDiff(chunk2, diffID)
	if len(block2.Hunks) != 2 {
		t.Errorf("Expected 2 hunks in chunk2, got %d", len(block2.Hunks))
	}

	// Verify both hunks are present
	if block2.Hunks[0].OldStart != 1 {
		t.Errorf("Expected first hunk at line 1, got %d", block2.Hunks[0].OldStart)
	}
	if block2.Hunks[1].OldStart != 20 {
		t.Errorf("Expected second hunk at line 20, got %d", block2.Hunks[1].OldStart)
	}
}
