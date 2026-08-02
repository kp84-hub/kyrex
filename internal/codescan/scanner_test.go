package codescan

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// ── Slop pattern tests ──────────────────────────────────────────────────────

func TestScanSlopPatterns_TODO(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "test.go")
	content := `package main

// TODO: implement error handling
func main() {
	println("hello") // TODO: remove debug
}
`
	if err := os.WriteFile(path, []byte(content), 0644); err != nil {
		t.Fatal(err)
	}

	result, err := Scan(path)
	if err != nil {
		t.Fatal(err)
	}

	// Should find at least 2 TODO hits
	todoCount := 0
	for _, f := range result.Findings {
		if strings.Contains(f.Message, "TODO") {
			todoCount++
		}
	}
	if todoCount < 1 {
		t.Errorf("expected TODO findings, got %d", todoCount)
	}
}

func TestScanSlopPatterns_FIXME(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "fixme.go")
	content := `package main

// FIXME: this is broken
func broken() {}
`
	if err := os.WriteFile(path, []byte(content), 0644); err != nil {
		t.Fatal(err)
	}

	result, err := Scan(path)
	if err != nil {
		t.Fatal(err)
	}

	hasFIXME := false
	for _, f := range result.Findings {
		if strings.Contains(f.Message, "FIXME") {
			hasFIXME = true
			break
		}
	}
	if !hasFIXME {
		t.Error("expected FIXME finding")
	}
}

func TestScanSlopPatterns_DebugPrint(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "debug.go")
	content := `package main

import "fmt"

func main() {
	fmt.Println("TEST: something")
}
`
	if err := os.WriteFile(path, []byte(content), 0644); err != nil {
		t.Fatal(err)
	}

	result, err := Scan(path)
	if err != nil {
		t.Fatal(err)
	}

	hasDebug := false
	for _, f := range result.Findings {
		if strings.Contains(f.Message, "debug print") {
			hasDebug = true
			break
		}
	}
	if !hasDebug {
		t.Error("expected debug print finding")
	}
}

// ── Dead code tests (Go) ───────────────────────────────────────────────────

func TestScanGoDeadCode_UnusedFunction(t *testing.T) {
	dir := t.TempDir()

	content := `package mypkg

// unused is defined but never called within this package
func unused() string {
	return "dead"
}

// used is called by UsedFunc below
func used() int {
	return 42
}

// UsedFunc is the public API — uses used()
func UsedFunc() int {
	return used()
}
`
	path := filepath.Join(dir, "stuff.go")
	if err := os.WriteFile(path, []byte(content), 0644); err != nil {
		t.Fatal(err)
	}

	report, err := ScanGoDeadCode(dir)
	if err != nil {
		t.Fatal(err)
	}

	// unused() should be reported
	found := false
	for _, uf := range report.UnusedFunctions {
		if uf.Name == "unused" {
			found = true
			break
		}
	}
	if !found {
		t.Error("expected 'unused' to be reported as dead code")
	}

	// used() should NOT be reported
	for _, uf := range report.UnusedFunctions {
		if uf.Name == "used" {
			t.Error("expected 'used' NOT to be reported as dead code")
		}
	}
}

// ── Large function tests ────────────────────────────────────────────────────

func TestScanLargeFunctions_ExceedsThreshold(t *testing.T) {
	dir := t.TempDir()

	// Build a function with many lines
	var lines []string
	lines = append(lines, `package mypkg`)
	lines = append(lines, `func hugeFunc() {`)
	for i := 0; i < LargeFuncThreshold+20; i++ {
		lines = append(lines, `	_ = i`)
	}
	lines = append(lines, `}`)

	content := strings.Join(lines, "\n")
	path := filepath.Join(dir, "huge.go")
	if err := os.WriteFile(path, []byte(content), 0644); err != nil {
		t.Fatal(err)
	}

	result, err := Scan(path)
	if err != nil {
		t.Fatal(err)
	}

	hasLarge := false
	for _, f := range result.Findings {
		if strings.Contains(f.Message, "lines") && strings.Contains(f.Message, "threshold") {
			hasLarge = true
			break
		}
	}
	if !hasLarge {
		t.Error("expected large function finding")
	}
}

// ── Duplicate import tests ─────────────────────────────────────────────────

func TestScanDuplicateImports_DuplicateFound(t *testing.T) {
	dir := t.TempDir()

	content := `package mypkg

import (
	"fmt"
	"fmt"
	"os"
)

func main() {
	fmt.Println(os.Args)
}
`
	path := filepath.Join(dir, "dupe.go")
	if err := os.WriteFile(path, []byte(content), 0644); err != nil {
		t.Fatal(err)
	}

	result, err := Scan(path)
	if err != nil {
		t.Fatal(err)
	}

	hasDupe := false
	for _, f := range result.Findings {
		if strings.Contains(f.Message, "Duplicate import") {
			hasDupe = true
			break
		}
	}
	if !hasDupe {
		t.Error("expected duplicate import finding")
	}
}

// ── Summary tests ──────────────────────────────────────────────────────────

func TestSummary_Clean(t *testing.T) {
	r := &ScanResult{ScannedFiles: 5}
	if !strings.Contains(r.Summary(), "clean") {
		t.Errorf("expected 'clean' in summary, got: %s", r.Summary())
	}
}

func TestSummary_WithIssues(t *testing.T) {
	r := &ScanResult{
		ScannedFiles:  3,
		SlopCount:     2,
		DeadCodeCount: 1,
	}
	s := r.Summary()
	if !strings.Contains(s, "2 slop") {
		t.Errorf("expected '2 slop' in summary, got: %s", s)
	}
	if !strings.Contains(s, "1 dead code") {
		t.Errorf("expected '1 dead code' in summary, got: %s", s)
	}
}

// ── Filter tests ───────────────────────────────────────────────────────────

func TestSlopOnly_FiltersCorrectly(t *testing.T) {
	r := &ScanResult{
		Findings: []Finding{
			{Category: CatSlop, Message: "todo"},
			{Category: CatDeadCode, Message: "unused"},
			{Category: CatSlop, Message: "fixme"},
		},
	}
	slop := r.SlopOnly()
	if len(slop) != 2 {
		t.Errorf("expected 2 slop findings, got %d", len(slop))
	}
}

func TestDeadCodeOnly_FiltersCorrectly(t *testing.T) {
	r := &ScanResult{
		Findings: []Finding{
			{Category: CatSlop, Message: "todo"},
			{Category: CatDeadCode, Message: "unused"},
			{Category: CatDeadCode, Message: "orphaned"},
		},
	}
	dead := r.DeadCodeOnly()
	if len(dead) != 2 {
		t.Errorf("expected 2 dead-code findings, got %d", len(dead))
	}
}

// ── Unused exports ─────────────────────────────────────────────────────────

func TestScanUnusedExports_UnusedExported(t *testing.T) {
	dir := t.TempDir()

	content := `package mypkg

// ExportedFunc is exported but never used in this package
func ExportedFunc() string {
	return "orphaned public API"
}

// usedInternal is only used internally
func usedInternal() int {
	return 1
}

// InternalCaller uses usedInternal
func InternalCaller() int {
	return usedInternal()
}
`
	path := filepath.Join(dir, "api.go")
	if err := os.WriteFile(path, []byte(content), 0644); err != nil {
		t.Fatal(err)
	}

	report, err := ScanUnusedExports(dir)
	if err != nil {
		t.Fatal(err)
	}

	// ExportedFunc should be flagged as unused
	found := false
	for _, uf := range report.UnusedFunctions {
		if uf.Name == "ExportedFunc" {
			found = true
			break
		}
	}
	if !found {
		t.Error("expected ExportedFunc to be flagged as unused export")
	}

	// InternalCaller should NOT be flagged — it's used (exported, but at least referenced somewhere)
	for _, uf := range report.UnusedFunctions {
		if uf.Name == "InternalCaller" {
			t.Error("expected InternalCaller NOT to be flagged as unused")
		}
	}
}

// ── Hardcoded secrets detection ────────────────────────────────────────────

func TestScanSlopPatterns_HardcodedSecret(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "secret.go")
	content := `package main

const apiKey = "sk-1234567890abcdef"
password := "supersecret"
`
	if err := os.WriteFile(path, []byte(content), 0644); err != nil {
		t.Fatal(err)
	}

	result, err := Scan(path)
	if err != nil {
		t.Fatal(err)
	}

	hasSecret := false
	for _, f := range result.Findings {
		if strings.Contains(f.Message, "hardcoded") {
			hasSecret = true
			break
		}
	}
	if !hasSecret {
		t.Error("expected hardcoded secret finding")
	}
}

// ── Edge case: empty file ──────────────────────────────────────────────────

func TestScan_EmptyFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "empty.go")
	if err := os.WriteFile(path, []byte(""), 0644); err != nil {
		t.Fatal(err)
	}

	result, err := Scan(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(result.Findings) != 0 {
		t.Errorf("expected 0 findings in empty file, got %d", len(result.Findings))
	}
	if result.ScannedFiles != 1 {
		t.Errorf("expected 1 scanned file, got %d", result.ScannedFiles)
	}
}

// ── Edge case: non-existent directory ──────────────────────────────────────

func TestScan_NonExistentPath(t *testing.T) {
	_, err := Scan("/nonexistent/path/that/does/not/exist")
	if err == nil {
		t.Error("expected error for non-existent path")
	}
}