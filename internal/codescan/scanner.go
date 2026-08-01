// Package codescan provides static analysis for detecting "slop" (low-quality
// patterns, TODOs, debug leftovers) and dead code (unused exports, orphaned
// functions) in Go and Python projects. It is designed to run after each commit
// — either as a git hook, a CI step, or an ad-hoc scan — and integrates with
// the existing rift git-diff infrastructure to analyze only changed files.
package codescan

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// ── Finding ─────────────────────────────────────────────────────────────────

// Severity classifies a finding.
type Severity int

const (
	SeverityWarn Severity = iota
	SeverityError
)

func (s Severity) String() string {
	switch s {
	case SeverityWarn:
		return "warn"
	case SeverityError:
		return "error"
	default:
		return "info"
	}
}

// Category classifies the kind of finding.
type Category string

const (
	CatSlop     Category = "slop"
	CatDeadCode Category = "dead-code"
)

// Finding is a single issue discovered during analysis.
type Finding struct {
	Path     string   `json:"path"`
	Line     int      `json:"line"`
	Column   int      `json:"column,omitempty"`
	Severity Severity `json:"severity"`
	Category Category `json:"category"`
	Message  string   `json:"message"`
	Match    string   `json:"match,omitempty"` // the offending text snippet
}

func (f Finding) String() string {
	sev := f.Severity.String()
	cat := string(f.Category)
	loc := f.Path
	if f.Line > 0 {
		loc = fmt.Sprintf("%s:%d", f.Path, f.Line)
	}
	return fmt.Sprintf("[%s/%s] %s: %s", sev, cat, loc, f.Message)
}

// ── Slop pattern checks ─────────────────────────────────────────────────────

// commonSlopPatterns lists regex patterns that indicate low-quality, debug, or
// incomplete code. Applied to all text files regardless of language.
var commonSlopPatterns = []struct {
	re      *regexp.Regexp
	message string
}{
	{regexp.MustCompile(`(?i)\bTODO\b`), "Leftover TODO — likely unfinished work"},
	{regexp.MustCompile(`(?i)\bFIXME\b`), "Leftover FIXME — known defect or incomplete fix"},
	{regexp.MustCompile(`(?i)\bHACK\b`), "Leftover HACK — indicates technical debt or workaround"},
	{regexp.MustCompile(`(?i)\bXXX\b`), "Leftover XXX — indicates problematic or dangerous code"},

	// Debug print statements
	{regexp.MustCompile(`fmt\.Print(ln|f)?\(.*debug|fmt\.Print(ln|f)?\(.*TEST`), "Stray debug print statement (fmt.Print) without proper gate"},
	{regexp.MustCompile(`print\(.*debug|print\(.*TEST`), "Stray debug print statement (print) without proper gate"},
	{regexp.MustCompile(`pp\.Print(ln|f)?\(`), "Stray debug print via pp.Print — likely leftover from debugging"},
	{regexp.MustCompile(`log\.Print(ln|f)?\(.*debug`), "Production log statement that may contain debug-only data"},
	{regexp.MustCompile(`os\.Exit\(0\)`), "os.Exit(0) in non-main package — prevents cleanup/defer from running"},

	// Hardcoded credentials or secrets risk
	{regexp.MustCompile(`(?i)(password|secret|api_key|apiKey)\s*[:=]\s*["'][^"']{3,}["']`), "Possible hardcoded credential or secret"},
}

// scanSlopPatterns scans a single file for all common slop patterns.
func scanSlopPatterns(path string) ([]Finding, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	lines := strings.Split(string(data), "\n")
	var findings []Finding

	for _, pat := range commonSlopPatterns {
		for i, line := range lines {
			locs := pat.re.FindAllStringIndex(line, -1)
			for _, loc := range locs {
				start, end := loc[0], loc[1]
				match := line[start:end]
				if len(match) > 80 {
					match = match[:80]
				}
				findings = append(findings, Finding{
					Path:     path,
					Line:     i + 1,
					Column:   start + 1,
					Severity: SeverityWarn,
					Category: CatSlop,
					Message:  pat.message,
					Match:    match,
				})
			}
		}
	}
	return findings, nil
}

// ── Dead code checks (Go) ───────────────────────────────────────────────────

// DeadCodeReport collects unused declarations across all Go files in a package.
type DeadCodeReport struct {
	UnusedFunctions []UnusedFunc `json:"unused_functions"`
}

// UnusedFunc describes an unused exported or unexported function.
type UnusedFunc struct {
	Name string `json:"name"`
	Path string `json:"path"`
	Line int    `json:"line"`
}

// ScanGoDeadCode parses all .go files in dir and reports functions that are
// defined but never referenced (excluding the entry-point package main).
// This is a conservative check: it tracks all function declarations and all
// call references, then reports declarations with zero references.
func ScanGoDeadCode(dir string) (*DeadCodeReport, error) {
	report := &DeadCodeReport{}

	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}

	fset := token.NewFileSet()
	decls := make(map[string]UnusedFunc)  // name -> declaration location
	refs := make(map[string]int)           // name -> reference count

	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".go") {
			continue
		}
		path := filepath.Join(dir, entry.Name())

		f, err := parser.ParseFile(fset, path, nil, 0)
		if err != nil {
			continue // skip unparseable files
		}

		// Only scan non-main packages (main packages may have dead-looking
		// bootstrap code that is actually the entry point)
		if f.Name.Name == "main" {
			continue
		}

		ast.Inspect(f, func(n ast.Node) bool {
			switch node := n.(type) {
			case *ast.FuncDecl:
				name := node.Name.Name
				if !ast.IsExported(name) {
					pos := fset.Position(node.Pos())
					decls[name] = UnusedFunc{
						Name: name,
						Path: path,
						Line: pos.Line,
					}
				}

			case *ast.CallExpr:
				// Track function call references
				switch fun := node.Fun.(type) {
				case *ast.Ident:
					refs[fun.Name]++
				case *ast.SelectorExpr:
					refs[fun.Sel.Name]++
				}

			case *ast.Ident:
				// Track identifier references (could be a function used
				// as a value, e.g. passed to another function)
				if node.Obj == nil && node.Name != "" {
					refs[node.Name]++
				}
			}
			return true
		})
	}

	for name, decl := range decls {
		// Allow init() and main() — these are special in Go
		if name == "init" || name == "main" {
			continue
		}
		// Only flag genuinely orphaned functions. refs counts every call /
		// value reference within the package; the declaration itself is NOT
		// a reference (FuncDecl never increments refs). So any refs > 0
		// means the function is actually used somewhere.
		if refs[name] == 0 {
			report.UnusedFunctions = append(report.UnusedFunctions, decl)
		}
	}

	return report, nil
}

// ── Dead export checks (unused exported symbols in non-main) ────────────────

// ScanUnusedExports reports exported functions in non-main packages that are
// not referenced anywhere else in the package. This catches dead public API
// surface that nothing calls — a stronger signal than private dead code.
func ScanUnusedExports(dir string) (*DeadCodeReport, error) {
	report := &DeadCodeReport{}

	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}

	fset := token.NewFileSet()
	exportedDecls := make(map[string]UnusedFunc)
	exportedFuncs := make(map[string]*ast.FuncDecl)
	allFns := make(map[string]bool)
	refs := make(map[string]int)

	// First pass: record declarations, every package-level function name,
	// and incoming references (calls / value uses) across the package.
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".go") {
			continue
		}
		path := filepath.Join(dir, entry.Name())

		f, err := parser.ParseFile(fset, path, nil, 0)
		if err != nil {
			continue
		}

		if f.Name.Name == "main" {
			continue
		}

		ast.Inspect(f, func(n ast.Node) bool {
			switch node := n.(type) {
			case *ast.FuncDecl:
				allFns[node.Name.Name] = true
				if ast.IsExported(node.Name.Name) {
					pos := fset.Position(node.Pos())
					exportedDecls[node.Name.Name] = UnusedFunc{
						Name: node.Name.Name,
						Path: path,
						Line: pos.Line,
					}
					exportedFuncs[node.Name.Name] = node
				}
				// A function's own definition is NOT an incoming reference,
				// so the FuncDecl name is intentionally not counted in refs.

			case *ast.CallExpr:
				switch fun := node.Fun.(type) {
				case *ast.Ident:
					refs[fun.Name]++
				case *ast.SelectorExpr:
					refs[fun.Sel.Name]++
				}

			case *ast.Ident:
				if node.Obj == nil && node.Name != "" {
					refs[node.Name]++
				}
			}
			return true
		})
	}

	for name, decl := range exportedDecls {
		if name == "init" || name == "main" {
			continue
		}
		// Referenced elsewhere in the package → definitely not dead.
		if refs[name] > 0 {
			continue
		}
		// A "live root": even if nothing references it, if it calls other
		// package functions it is an entry point / not isolated. Only flag
		// exports that are completely disconnected (no incoming refs AND no
		// outgoing calls to other functions in this package).
		if fn := exportedFuncs[name]; fn != nil && fn.Body != nil &&
			fnCallsPackageFunc(fn.Body, allFns) {
			continue
		}
		report.UnusedFunctions = append(report.UnusedFunctions, decl)
	}

	return report, nil
}

// fnCallsPackageFunc reports whether body calls any function whose name is in
// allFns (i.e. any function declared in the same package).
func fnCallsPackageFunc(body *ast.BlockStmt, allFns map[string]bool) bool {
	calls := false
	ast.Inspect(body, func(n ast.Node) bool {
		ce, ok := n.(*ast.CallExpr)
		if !ok {
			return true
		}
		switch fun := ce.Fun.(type) {
		case *ast.Ident:
			if allFns[fun.Name] {
				calls = true
				return false
			}
		case *ast.SelectorExpr:
			if allFns[fun.Sel.Name] {
				calls = true
				return false
			}
		}
		return true
	})
	return calls
}

// ── Large function detection ────────────────────────────────────────────────

// LargeFuncThreshold is the max line count for a single function before a
// warning is raised. Functions exceeding this are likely too complex.
const LargeFuncThreshold = 80

// ScanLargeFunctions parses Go files and reports functions exceeding the
// threshold. Excessively large functions are a strong "slop" indicator.
func ScanLargeFunctions(dir string) ([]Finding, error) {
	var findings []Finding

	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}

	fset := token.NewFileSet()
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".go") {
			continue
		}
		path := filepath.Join(dir, entry.Name())

		f, err := parser.ParseFile(fset, path, nil, 0)
		if err != nil {
			continue
		}

		findings = append(findings, largeFunctionsInFile(path, fset, f)...)
	}

	return findings, nil
}

// largeFunctionsInFile reports functions exceeding the threshold in a single
// parsed Go file.
func largeFunctionsInFile(path string, fset *token.FileSet, f *ast.File) []Finding {
	var findings []Finding
	ast.Inspect(f, func(n ast.Node) bool {
		fn, ok := n.(*ast.FuncDecl)
		if !ok {
			return true
		}
		start := fset.Position(fn.Pos()).Line
		end := fset.Position(fn.End()).Line
		lines := end - start + 1
		if lines > LargeFuncThreshold {
			findings = append(findings, Finding{
				Path:     path,
				Line:     start,
				Severity: SeverityWarn,
				Category: CatSlop,
				Message:  fmt.Sprintf("Function %s is %d lines (threshold: %d) — consider refactoring", fn.Name.Name, lines, LargeFuncThreshold),
			})
		}
		return true
	})
	return findings
}

// scanLargeFunctionsInFile parses a single Go file and returns its
// large-function findings. Used by Scan when given a single .go file.
func scanLargeFunctionsInFile(path string) ([]Finding, error) {
	fset := token.NewFileSet()
	f, err := parser.ParseFile(fset, path, nil, 0)
	if err != nil {
		return nil, err
	}
	return largeFunctionsInFile(path, fset, f), nil
}

// ── Duplicate imports ───────────────────────────────────────────────────────

// ScanDuplicateImports checks Go files for the same import path listed more
// than once — a clear slop signal from copy-paste or messy merging.
func ScanDuplicateImports(dir string) ([]Finding, error) {
	var findings []Finding

	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}

	fset := token.NewFileSet()
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".go") {
			continue
		}
		path := filepath.Join(dir, entry.Name())

		f, err := parser.ParseFile(fset, path, nil, 0)
		if err != nil {
			continue
		}

		findings = append(findings, duplicateImportsInFile(path, fset, f)...)
	}

	return findings, nil
}

// duplicateImportsInFile reports duplicate import paths within a single parsed
// Go file.
func duplicateImportsInFile(path string, fset *token.FileSet, f *ast.File) []Finding {
	var findings []Finding
	seen := make(map[string]token.Pos)
	for _, imp := range f.Imports {
		impPath := strings.Trim(imp.Path.Value, `"`)
		if pos, ok := seen[impPath]; ok {
			firstLine := fset.Position(pos).Line
			dupLine := fset.Position(imp.Pos()).Line
			findings = append(findings, Finding{
				Path:     path,
				Line:     dupLine,
				Severity: SeverityWarn,
				Category: CatSlop,
				Message:  fmt.Sprintf("Duplicate import %q (first at line %d)", impPath, firstLine),
				Match:    impPath,
			})
		} else {
			seen[impPath] = imp.Pos()
		}
	}
	return findings
}

// scanDuplicateImportsInFile parses a single Go file and returns its duplicate
// import findings. Used by Scan when given a single .go file.
func scanDuplicateImportsInFile(path string) ([]Finding, error) {
	fset := token.NewFileSet()
	f, err := parser.ParseFile(fset, path, nil, 0)
	if err != nil {
		return nil, err
	}
	return duplicateImportsInFile(path, fset, f), nil
}

// ── Composite scanner ───────────────────────────────────────────────────────

// ScanResult aggregates all findings for a single scan operation.
type ScanResult struct {
	Findings       []Finding        `json:"findings"`
	SlopCount      int              `json:"slop_count"`
	DeadCodeCount  int              `json:"dead_code_count"`
	ScannedFiles   int              `json:"scanned_files"`
	DeadCodeReport *DeadCodeReport  `json:"dead_code_report,omitempty"`
}

// Scan runs all checks on the given directory (or a single file). If paths is
// empty, it scans the current directory recursively.
func Scan(paths ...string) (*ScanResult, error) {
	if len(paths) == 0 {
		paths = []string{"."}
	}

	result := &ScanResult{}

	for _, path := range paths {
		info, err := os.Stat(path)
		if err != nil {
			return nil, fmt.Errorf("codescan: %w", err)
		}

		if info.IsDir() {
			// Walk directory recursively
			if err := filepath.WalkDir(path, func(p string, d os.DirEntry, err error) error {
				if err != nil {
					return nil // skip inaccessible files
				}
				if d.IsDir() {
					// Skip common generated/vendor directories
					base := d.Name()
					if base == ".git" || base == "node_modules" || base == ".venv" ||
						base == "venv" || base == "__pycache__" || base == "target" ||
						base == "dist" || base == "build" || base == ".rifts" {
						return filepath.SkipDir
					}
					return nil
				}
				return scanFile(p, path, result)
			}); err != nil {
				return nil, err
			}

			// Run Go-specific dead code analysis on the top-level dir
			if hasGoFiles(path) {
				report, err := ScanGoDeadCode(path)
				if err == nil && len(report.UnusedFunctions) > 0 {
					result.DeadCodeReport = report
					for _, uf := range report.UnusedFunctions {
						result.Findings = append(result.Findings, Finding{
							Path:     uf.Path,
							Line:     uf.Line,
							Severity: SeverityError,
							Category: CatDeadCode,
							Message:  fmt.Sprintf("Unused function %s — no references in this package", uf.Name),
							Match:    uf.Name,
						})
					}
				}

				// Unused exports check
				exportReport, err := ScanUnusedExports(path)
				if err == nil && len(exportReport.UnusedFunctions) > 0 {
					if result.DeadCodeReport == nil {
						result.DeadCodeReport = exportReport
					} else {
						result.DeadCodeReport.UnusedFunctions = append(
							result.DeadCodeReport.UnusedFunctions,
							exportReport.UnusedFunctions...,
						)
					}
					for _, uf := range exportReport.UnusedFunctions {
						result.Findings = append(result.Findings, Finding{
							Path:     uf.Path,
							Line:     uf.Line,
							Severity: SeverityError,
							Category: CatDeadCode,
							Message:  fmt.Sprintf("Unused exported function %s — no references in this package", uf.Name),
							Match:    uf.Name,
						})
					}
				}

				// Large function detection
				largeFuncs, _ := ScanLargeFunctions(path)
				result.Findings = append(result.Findings, largeFuncs...)

				// Duplicate imports
				dupImports, _ := ScanDuplicateImports(path)
				result.Findings = append(result.Findings, dupImports...)
			}
		} else {
			// Single file
			if err := scanFile(path, filepath.Dir(path), result); err != nil {
				return nil, err
			}
			// For a single .go file, also run the Go-specific structural
			// checks so Scan(path) reports large functions and duplicate
			// imports on individual files, not just directories.
			if strings.HasSuffix(filepath.Base(path), ".go") {
				if large, err := scanLargeFunctionsInFile(path); err == nil {
					result.Findings = append(result.Findings, large...)
				}
				if dup, err := scanDuplicateImportsInFile(path); err == nil {
					result.Findings = append(result.Findings, dup...)
				}
			}
		}
	}

	for _, f := range result.Findings {
		switch f.Category {
		case CatSlop:
			result.SlopCount++
		case CatDeadCode:
			result.DeadCodeCount++
		}
	}

	return result, nil
}

// scanFile runs text-based slop pattern checks on a single file.
func scanFile(path, root string, result *ScanResult) error {
	ext := strings.ToLower(filepath.Ext(path))
	// Only scan text-like source files
	switch ext {
	case ".go", ".py", ".js", ".ts", ".tsx", ".jsx", ".rs", ".rb", ".sh",
		".yaml", ".yml", ".json", ".toml", ".md", ".html", ".css", ".sql",
		".c", ".h", ".cpp", ".hpp", ".java", ".kt", ".swift":
	default:
		return nil // skip binary or unknown formats
	}

	// Skip generated or vendored files
	base := filepath.Base(path)
	if strings.HasPrefix(base, ".") || strings.HasPrefix(base, "_") {
		return nil
	}
	if strings.Contains(path, "vendor/") || strings.Contains(path, "node_modules/") {
		return nil
	}

	findings, err := scanSlopPatterns(path)
	if err != nil {
		return nil // skip files we can't read
	}

	result.ScannedFiles++
	result.Findings = append(result.Findings, findings...)
	return nil
}

// hasGoFiles returns true if dir contains at least one .go file.
func hasGoFiles(dir string) bool {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return false
	}
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(e.Name(), ".go") {
			return true
		}
	}
	return false
}

// ── Report formatters ───────────────────────────────────────────────────────

// Summary returns a human-readable one-line summary of scan results.
func (r *ScanResult) Summary() string {
	parts := []string{fmt.Sprintf("Scanned %d files", r.ScannedFiles)}
	if r.SlopCount > 0 {
		parts = append(parts, fmt.Sprintf("%d slop issue(s)", r.SlopCount))
	}
	if r.DeadCodeCount > 0 {
		parts = append(parts, fmt.Sprintf("%d dead code finding(s)", r.DeadCodeCount))
	}
	if r.SlopCount == 0 && r.DeadCodeCount == 0 {
		parts = append(parts, "clean — no issues found")
	}
	return strings.Join(parts, ", ")
}

// SlopOnly returns only findings categorized as slop.
func (r *ScanResult) SlopOnly() []Finding {
	var out []Finding
	for _, f := range r.Findings {
		if f.Category == CatSlop {
			out = append(out, f)
		}
	}
	return out
}

// DeadCodeOnly returns only findings categorized as dead code.
func (r *ScanResult) DeadCodeOnly() []Finding {
	var out []Finding
	for _, f := range r.Findings {
		if f.Category == CatDeadCode {
			out = append(out, f)
		}
	}
	return out
}