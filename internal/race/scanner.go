// Package race scanner provides static analysis of Go source files for
// common code quality issues: dead (unused) code, oversized functions,
// duplicate imports, and unused exported symbols.
package race

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strings"
)

// ScanResult represents a single issue found during scanning.
type ScanResult struct {
	File    string
	Line    int
	Message string
}

// ScannerConfig holds configuration for the scanner.
type ScannerConfig struct {
	LargeFuncThreshold int // minimum lines for a function to be considered "large"
}

// DefaultScannerConfig returns sensible defaults.
func DefaultScannerConfig() ScannerConfig {
	return ScannerConfig{
		LargeFuncThreshold: 50,
	}
}

// scanFiles parses every .go file in dir and returns the file set + ASTs.
// Files that fail to parse are silently skipped.
func scanFiles(dir string) (*token.FileSet, []*ast.File, error) {
	fset := token.NewFileSet()
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, nil, fmt.Errorf("scan: read dir %s: %w", dir, err)
	}

	var files []*ast.File
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".go") {
			continue
		}
		path := filepath.Join(dir, entry.Name())
		f, err := parser.ParseFile(fset, path, nil, parser.AllErrors)
		if err != nil {
			continue
		}
		files = append(files, f)
	}
	return fset, files, nil
}

// ScanGoDeadCode finds unexported functions that are declared but never
// called anywhere in the package. Special functions (init, main) and
// exported functions are excluded.
func ScanGoDeadCode(dir string) ([]ScanResult, error) {
	fset, files, err := scanFiles(dir)
	if err != nil {
		return nil, err
	}

	// Collect all function declarations and all function call references.
	funcDecls := make(map[string]*ast.FuncDecl)
	callRefs := make(map[string]bool)

	for _, f := range files {
		ast.Inspect(f, func(n ast.Node) bool {
			switch node := n.(type) {
			case *ast.FuncDecl:
				funcDecls[node.Name.Name] = node
			case *ast.CallExpr:
				switch fun := node.Fun.(type) {
				case *ast.Ident:
					callRefs[fun.Name] = true
				case *ast.SelectorExpr:
					callRefs[fun.Sel.Name] = true
				}
			}
			return true
		})
	}

	var results []ScanResult
	for name, decl := range funcDecls {
		if name == "init" || name == "main" || ast.IsExported(name) {
			continue
		}
		if callRefs[name] {
			continue
		}
		pos := fset.Position(decl.Pos())
		results = append(results, ScanResult{
			File:    pos.Filename,
			Line:    pos.Line,
			Message: fmt.Sprintf("unused function: %s", name),
		})
	}
	return results, nil
}

// ScanLargeFunctions finds functions whose body (between braces, exclusive)
// exceeds the given line threshold. threshold <= 0 uses the default of 50.
func ScanLargeFunctions(dir string, threshold int) ([]ScanResult, error) {
	if threshold <= 0 {
		threshold = DefaultScannerConfig().LargeFuncThreshold
	}

	fset, files, err := scanFiles(dir)
	if err != nil {
		return nil, err
	}

	var results []ScanResult
	for _, f := range files {
		ast.Inspect(f, func(n ast.Node) bool {
			fn, ok := n.(*ast.FuncDecl)
			if !ok || fn.Body == nil {
				return true
			}
			start := fset.Position(fn.Body.Lbrace).Line
			end := fset.Position(fn.Body.Rbrace).Line
			bodyLines := end - start - 1 // exclude the brace lines themselves
			if bodyLines > threshold {
				pos := fset.Position(fn.Pos())
				results = append(results, ScanResult{
					File:    pos.Filename,
					Line:    pos.Line,
					Message: fmt.Sprintf("function %s is %d lines (threshold: %d)", fn.Name.Name, bodyLines, threshold),
				})
			}
			return true
		})
	}
	return results, nil
}

// ScanDuplicateImports finds import paths that appear more than once
// within the same file.
func ScanDuplicateImports(dir string) ([]ScanResult, error) {
	fset, files, err := scanFiles(dir)
	if err != nil {
		return nil, err
	}

	var results []ScanResult
	for _, f := range files {
		seen := make(map[string]bool)
		for _, decl := range f.Decls {
			gen, ok := decl.(*ast.GenDecl)
			if !ok || gen.Tok != token.IMPORT {
				continue
			}
			for _, spec := range gen.Specs {
				imp, ok := spec.(*ast.ImportSpec)
				if !ok {
					continue
				}
				path := strings.Trim(imp.Path.Value, `"`)
				if seen[path] {
					pos := fset.Position(imp.Pos())
					results = append(results, ScanResult{
						File:    pos.Filename,
						Line:    pos.Line,
						Message: fmt.Sprintf("duplicate import: %s", path),
					})
				} else {
					seen[path] = true
				}
			}
		}
	}
	return results, nil
}

// ScanUnusedExports finds exported (capitalized) names that are declared
// but never referenced within the same package. Excludes init and main.
func ScanUnusedExports(dir string) ([]ScanResult, error) {
	fset, files, err := scanFiles(dir)
	if err != nil {
		return nil, err
	}

	// First pass: collect all exported declarations (funcs, vars, consts, types).
	type declInfo struct {
		node ast.Node
	}
	exports := make(map[string]*declInfo)

	for _, f := range files {
		ast.Inspect(f, func(n ast.Node) bool {
			switch node := n.(type) {
			case *ast.FuncDecl:
				if ast.IsExported(node.Name.Name) {
					exports[node.Name.Name] = &declInfo{node: node}
				}
			case *ast.GenDecl:
				for _, spec := range node.Specs {
					switch s := spec.(type) {
					case *ast.ValueSpec:
						for _, name := range s.Names {
							if ast.IsExported(name.Name) {
								exports[name.Name] = &declInfo{node: s, file: f}
							}
						}
					case *ast.TypeSpec:
						if ast.IsExported(s.Name.Name) {
							exports[s.Name.Name] = &declInfo{node: s, file: f}
						}
					}
				}
			}
			return true
		})
	}

	// Second pass: collect actual identifier references, skipping declaration
	// names by walking statement bodies and expressions (not declarations).
	refs := make(map[string]bool)
	for _, f := range files {
		for _, decl := range f.Decls {
			collectRefs(decl, &refs)
		}
	}

	var results []ScanResult
	for name, info := range exports {
		if name == "init" || name == "main" {
			continue
		}
		if refs[name] {
			continue
		}
		pos := fset.Position(info.node.Pos())
		results = append(results, ScanResult{
			File:    pos.Filename,
			Line:    pos.Line,
			Message: fmt.Sprintf("unused export: %s", name),
		})
	}
	return results, nil
}

// collectRefs walks an AST node and records all ident references (not
// definitions) into refs.
func collectRefs(n ast.Node, refs *map[string]bool) {
	ast.Inspect(n, func(child ast.Node) bool {
		switch node := child.(type) {
		case *ast.FuncDecl:
			// The function name itself is a definition; skip its ident
			// but walk the body and receiver/params.
			if node.Recv != nil {
				ast.Inspect(node.Recv, func(c ast.Node) bool {
					if id, ok := c.(*ast.Ident); ok && id.Name != "_" {
						(*refs)[id.Name] = true
					}
					return true
				})
			}
			if node.Type.Params != nil {
				for _, p := range node.Type.Params.List {
					ast.Inspect(p.Type, func(c ast.Node) bool {
						if id, ok := c.(*ast.Ident); ok && id.Name != "_" {
							(*refs)[id.Name] = true
						}
						return true
					})
				}
			}
			if node.Type.Results != nil {
				for _, p := range node.Type.Results.List {
					ast.Inspect(p.Type, func(c ast.Node) bool {
						if id, ok := c.(*ast.Ident); ok && id.Name != "_" {
							(*refs)[id.Name] = true
						}
						return true
					})
				}
			}
			if node.Body != nil {
				collectRefsFromStmts(node.Body.List, refs)
			}
			return false // handled manually
		case *ast.GenDecl:
			for _, spec := range node.Specs {
				switch s := spec.(type) {
				case *ast.ValueSpec:
					for _, v := range s.Values {
						collectRefsFromExpr(v, refs)
					}
					if s.Type != nil {
						collectRefsFromExpr(s.Type, refs)
					}
				case *ast.TypeSpec:
					collectRefsFromExpr(s.Type, refs)
				}
			}
			return false
		case *ast.Ident:
			if node.Name != "_" {
				(*refs)[node.Name] = true
			}
			return true
		}
		return true
	})
}

// collectRefsFromStmts walks statement lists for identifier references.
func collectRefsFromStmts(stmts []ast.Stmt, refs *map[string]bool) {
	for _, s := range stmts {
		collectRefsFromStmt(s, refs)
	}
}

// collectRefsFromStmt walks a single statement for identifier references.
func collectRefsFromStmt(s ast.Stmt, refs *map[string]bool) {
	switch node := s.(type) {
	case *ast.ExprStmt:
		collectRefsFromExpr(node.X, refs)
	case *ast.AssignStmt:
		for _, r := range node.Rhs {
			collectRefsFromExpr(r, refs)
		}
	case *ast.ReturnStmt:
		for _, r := range node.Results {
			collectRefsFromExpr(r, refs)
		}
	case *ast.IfStmt:
		collectRefsFromExpr(node.Cond, refs)
		collectRefsFromStmts(node.Body.List, refs)
		if node.Else != nil {
			collectRefsFromStmt(node.Else, refs)
		}
	case *ast.ForStmt:
		collectRefsFromExpr(node.Cond, refs)
		collectRefsFromStmts(node.Body.List, refs)
	case *ast.RangeStmt:
		collectRefsFromExpr(node.X, refs)
		collectRefsFromStmts(node.Body.List, refs)
	case *ast.BlockStmt:
		collectRefsFromStmts(node.List, refs)
	case *ast.SwitchStmt:
		collectRefsFromExpr(node.Tag, refs)
		for _, cc := range node.Body.List {
			if cas, ok := cc.(*ast.CaseClause); ok {
				collectRefsFromStmts(cas.Body, refs)
			}
		}
	case *ast.DeclStmt:
		collectRefs(node.Decl, refs)
	case *ast.GoStmt:
		collectRefsFromExpr(node.Call, refs)
	case *ast.DeferStmt:
		collectRefsFromExpr(node.Call, refs)
	case *ast.SendStmt:
		collectRefsFromExpr(node.Chan, refs)
		collectRefsFromExpr(node.Value, refs)
	}
}

// collectRefsFromExpr walks an expression for identifier references.
func collectRefsFromExpr(e ast.Expr, refs *map[string]bool) {
	if e == nil {
		return
	}
	ast.Inspect(e, func(n ast.Node) bool {
		id, ok := n.(*ast.Ident)
		if !ok {
			return true
		}
		if id.Name != "_" {
			(*refs)[id.Name] = true
		}
		return true
	})
}