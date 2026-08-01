// codescan is a post-commit static analysis tool that scans code for slop
// (TODOs, debug prints, copy-paste leftovers) and dead code (unused functions,
// orphaned exports). It's designed to run after every commit — either as a
// local git hook or as a CI step.
//
// Usage:
//
//	codescan [paths...]
//	  Scan one or more paths (files or directories). If no paths given,
//	  scans the current directory recursively.
//
//	codescan --diff [ref]
//	  Scan only files changed since ref (default: HEAD^). Uses git diff
//	  to get the changed file list, then scans only those files.
//
//	codescan --ci
//	  CI-friendly mode: scans recursively, outputs JSON, exits code 1 if
//	  any error-severity findings exist.
//
//	codescan --summary
//	  Print a one-line summary only.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/kp84-hub/kx/internal/codescan"
)

func main() {
	diffMode := flag.String("diff", "", "Scan files changed since a git ref (e.g. HEAD^, origin/main)")
	ciMode := flag.Bool("ci", false, "CI mode: output JSON, exit 1 on errors")
	summaryOnly := flag.Bool("summary", false, "Print one-line summary only")
	flag.Parse()

	os.Exit(run(*diffMode, *ciMode, *summaryOnly, flag.Args()))
}

func run(diffRef string, ciMode, summaryOnly bool, paths []string) int {
	var result *codescan.ScanResult
	var err error

	if diffRef != "" {
		// ── Git diff mode: scan only changed files ──
		changedFiles, err := getChangedFiles(diffRef)
		if err != nil {
			fmt.Fprintf(os.Stderr, "codescan: failed to get changed files: %v\n", err)
			return 1
		}
		if len(changedFiles) == 0 {
			fmt.Fprintln(os.Stderr, "codescan: no changed files to scan")
			return 0
		}

		// Resolve relative paths to absolute
		wd, _ := os.Getwd()
		for i, f := range changedFiles {
			if !filepath.IsAbs(f) {
				changedFiles[i] = filepath.Join(wd, f)
			}
		}
		result, err = codescan.Scan(changedFiles...)
	} else if len(paths) > 0 {
		result, err = codescan.Scan(paths...)
	} else {
		result, err = codescan.Scan(".")
	}

	if err != nil {
		fmt.Fprintf(os.Stderr, "codescan: scan error: %v\n", err)
		return 1
	}

	// ── Output ──
	if ciMode {
		// JSON output for CI consumption
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		if err := enc.Encode(result); err != nil {
			fmt.Fprintf(os.Stderr, "codescan: JSON output error: %v\n", err)
			return 1
		}
	} else if summaryOnly {
		fmt.Println(result.Summary())
		// If there are error-severity findings, list them
		for _, f := range result.Findings {
			if f.Severity == codescan.SeverityError {
				fmt.Println("  ", f.String())
			}
		}
	} else {
		// Default human-readable output
		fmt.Println(result.Summary())
		fmt.Println()
		for _, f := range result.Findings {
			fmt.Println(f.String())
		}
	}

	// ── Exit code ──
	if ciMode {
		// In CI mode, exit 1 if any error-level findings exist
		for _, f := range result.Findings {
			if f.Severity == codescan.SeverityError {
				return 1
			}
		}
	}

	return 0
}

// getChangedFiles returns the list of files that differ from the given ref.
// It combines tracked changes (including staged) and untracked files.
func getChangedFiles(ref string) ([]string, error) {
	// Files changed between ref and working tree
	cmd := exec.Command("git", "diff", "--name-only", ref, "--")
	out, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("git diff --name-only %s: %w", ref, err)
	}
	changed := strings.Fields(string(out))

	// Untracked files (not in any diff)
	cmd = exec.Command("git", "ls-files", "--others", "--exclude-standard")
	out, err = cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("git ls-files --others: %w", err)
	}
	untracked := strings.Fields(string(out))
	changed = append(changed, untracked...)

	// Deduplicate
	seen := make(map[string]bool)
	unique := make([]string, 0, len(changed))
	for _, f := range changed {
		if !seen[f] {
			seen[f] = true
			unique = append(unique, f)
		}
	}
	return unique, nil
}