package race

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
	"time"
)

// Manifest records the metadata of a running or abandoned race. It is written
// to <race-dir>/manifest.json after lanes are spawned so that crash-recovery
// can detect orphaned races on startup.
type Manifest struct {
	Task      string    `json:"task"`
	SrcDir    string    `json:"src_dir"`
	RaceDir   string    `json:"race_dir"`
	PIDs      []int     `json:"pids"`
	StartedAt time.Time `json:"started_at"`
}

// WriteManifest writes a manifest.json file into the race directory containing
// the engine PID for every non-nil lane whose cmd.Process is available.
// Callers should invoke this after all lanes have been spawned.
func (r *Race) WriteManifest() error {
	var pids []int
	for _, l := range r.Lanes {
		if l == nil || l.cmd == nil || l.cmd.Process == nil {
			continue
		}
		pids = append(pids, l.cmd.Process.Pid)
	}

	m := Manifest{
		Task:      r.Task,
		SrcDir:    r.SrcDir,
		RaceDir:   r.Dir,
		PIDs:      pids,
		StartedAt: r.StartedAt,
	}

	b, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		return fmt.Errorf("race manifest: marshal: %w", err)
	}
	b = append(b, '\n')

	path := filepath.Join(r.Dir, "manifest.json")
	if err := os.WriteFile(path, b, 0644); err != nil {
		return fmt.Errorf("race manifest: write: %w", err)
	}
	return nil
}

// FindAbandoned scans parent's immediate subdirectories for manifest.json
// files and returns those whose PIDs are ALL dead (i.e., no engine process
// is still running). It is meant for startup crash-recovery: races that were
// orphaned by power loss or a hard kill where Cleanup never ran.
//
// Rules:
//   - A missing or unparseable manifest is silently skipped — it is not
//     considered a race-owned directory.
//   - A missing parent directory returns (nil, nil), not an error.
//   - This function never errors; it returns candidates and leaves the
//     deletion decision to the caller.
func FindAbandoned(parent string) ([]Manifest, error) {
	entries, err := os.ReadDir(parent)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}

	var abandoned []Manifest

	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}

		manifestPath := filepath.Join(parent, entry.Name(), "manifest.json")
		data, err := os.ReadFile(manifestPath)
		if err != nil {
			// Missing or unreadable — skip silently
			continue
		}

		var m Manifest
		if err := json.Unmarshal(data, &m); err != nil {
			// Unparseable — skip silently
			continue
		}

		// If this manifest has no PIDs, it is not a fully-spawned race
		if len(m.PIDs) == 0 {
			continue
		}

		// Check every PID; if any is alive, this race is not abandoned
		allDead := true
		for _, pid := range m.PIDs {
			if err := syscall.Kill(pid, 0); err == nil {
				// Process exists — race still alive
				allDead = false
				break
			}
		}

		if allDead {
			abandoned = append(abandoned, m)
		}
	}

	return abandoned, nil
}