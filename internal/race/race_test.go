package race

import (
	"os"
	"path/filepath"
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

// ── Scanner tests ──────────────────────────────────────────────────────────

func TestScanGoDeadCode_UnusedFunction(t *testing.T) {
	dir := t.TempDir()

	code := `package testdata

func usedFunc() {}

func caller() {
	usedFunc()
}

func unusedFunc() {}
`
	if err := os.WriteFile(filepath.Join(dir, "main.go"), []byte(code), 0644); err != nil {
		t.Fatal(err)
	}

	results, err := ScanGoDeadCode(dir)
	if err != nil {
		t.Fatalf("ScanGoDeadCode error: %v", err)
	}

	if len(results) != 1 {
		t.Fatalf("expected 1 unused function, got %d: %+v", len(results), results)
	}
	if results[0].Message != "unused function: unusedFunc" {
		t.Errorf("expected message about unusedFunc, got %q", results[0].Message)
	}
}

func TestScanLargeFunctions_ExceedsThreshold(t *testing.T) {
	dir := t.TempDir()

	// Build a function body with >50 lines (51 blank lines of comments inside braces).
	var lines []byte
	lines = append(lines, []byte("package testdata\n\nfunc smallFunc() {}\n\nfunc largeFunc() {\n")...)
	for i := 0; i < 52; i++ {
		lines = append(lines, []byte("\t// line\n")...)
	}
	lines = append(lines, []byte("}\n")...)

	if err := os.WriteFile(filepath.Join(dir, "main.go"), lines, 0644); err != nil {
		t.Fatal(err)
	}

	results, err := ScanLargeFunctions(dir, 50)
	if err != nil {
		t.Fatalf("ScanLargeFunctions error: %v", err)
	}

	if len(results) != 1 {
		t.Fatalf("expected 1 large function, got %d: %+v", len(results), results)
	}
	if results[0].Message != "function largeFunc is 52 lines (threshold: 50)" {
		t.Errorf("unexpected message: %q", results[0].Message)
	}
}

func TestScanDuplicateImports_DuplicateFound(t *testing.T) {
	dir := t.TempDir()

	code := `package testdata

import (
	"fmt"
	"os"
	"fmt"
)

func f() { fmt.Println("hello") }
`
	if err := os.WriteFile(filepath.Join(dir, "main.go"), []byte(code), 0644); err != nil {
		t.Fatal(err)
	}

	results, err := ScanDuplicateImports(dir)
	if err != nil {
		t.Fatalf("ScanDuplicateImports error: %v", err)
	}

	if len(results) != 1 {
		t.Fatalf("expected 1 duplicate import, got %d: %+v", len(results), results)
	}
	if results[0].Message != "duplicate import: fmt" {
		t.Errorf("expected message about fmt, got %q", results[0].Message)
	}
}

func TestScanUnusedExports_UnusedExported(t *testing.T) {
	dir := t.TempDir()

	code := `package testdata

// UsedExported is called below.
func UsedExported() {}

// UnusedExported is never referenced.
func UnusedExported() {}

// caller references UsedExported.
func caller() {
	UsedExported()
}
`
	if err := os.WriteFile(filepath.Join(dir, "main.go"), []byte(code), 0644); err != nil {
		t.Fatal(err)
	}

	results, err := ScanUnusedExports(dir)
	if err != nil {
		t.Fatalf("ScanUnusedExports error: %v", err)
	}

	if len(results) != 1 {
		t.Fatalf("expected 1 unused export, got %d: %+v", len(results), results)
	}
	if results[0].Message != "unused export: UnusedExported" {
		t.Errorf("expected message about UnusedExported, got %q", results[0].Message)
	}
}
