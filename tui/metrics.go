package tui

import (
	"fmt"
	"os"
	"sort"
	"strings"
	"sync"
	"time"
)

// RenderMetrics collects timing and frequency data for the Bubble Tea render loop.
// Thread-safe: View() and Update() may be called from different goroutines.
type RenderMetrics struct {
	mu sync.Mutex

	// View() timing
	viewCallCount int64
	viewTotalNs   int64
	viewMaxNs     int64
	viewMinNs     int64
	viewSamples   []int64 // last N samples for percentile calc

	// FullViewportContent() timing
	fvcCallCount   int64
	fvcTotalNs     int64
	fvcMaxNs       int64
	fvcCacheHits   int64
	fvcCacheMisses int64

	// SetContent tracking
	setContentCalls int64
	setContentSkips int64 // skipped because content unchanged

	// Message counts by type
	msgCounts map[string]int64

	// Redraw tracking: timestamps of View() calls for rate calculation
	viewTimestamps []time.Time

	// Update() calls that set _viewportDirty = true
	dirtyTriggers int64

	// Session timing
	sessionStart time.Time
	lastViewTime time.Time

	// Per-message-type render trigger counts
	// (how many times each msg type caused _viewportDirty=true)
	dirtyByMsgType map[string]int64
}

func NewRenderMetrics() *RenderMetrics {
	return &RenderMetrics{
		msgCounts:      make(map[string]int64),
		dirtyByMsgType: make(map[string]int64),
		viewSamples:    make([]int64, 0, 1000),
		viewTimestamps: make([]time.Time, 0, 1000),
		sessionStart:   time.Now(),
	}
}

// RecordView records a View() call with its duration.
func (rm *RenderMetrics) RecordView(d time.Duration) {
	rm.mu.Lock()
	defer rm.mu.Unlock()

	ns := d.Nanoseconds()
	rm.viewCallCount++
	rm.viewTotalNs += ns
	if ns > rm.viewMaxNs {
		rm.viewMaxNs = ns
	}
	if rm.viewMinNs == 0 || ns < rm.viewMinNs {
		rm.viewMinNs = ns
	}

	// Keep last 500 samples for percentile
	if len(rm.viewSamples) >= 500 {
		rm.viewSamples = rm.viewSamples[1:]
	}
	rm.viewSamples = append(rm.viewSamples, ns)

	now := time.Now()
	rm.lastViewTime = now
	if len(rm.viewTimestamps) >= 500 {
		rm.viewTimestamps = rm.viewTimestamps[1:]
	}
	rm.viewTimestamps = append(rm.viewTimestamps, now)
}

// RecordMsg records a message arriving in Update().
func (rm *RenderMetrics) RecordMsg(msgType string, causedDirty bool) {
	rm.mu.Lock()
	defer rm.mu.Unlock()

	rm.msgCounts[msgType]++
	if causedDirty {
		rm.dirtyTriggers++
		rm.dirtyByMsgType[msgType]++
	}
}

// RecordFVC records a FullViewportContent() call.
func (rm *RenderMetrics) RecordFVC(d time.Duration, cacheHit bool) {
	rm.mu.Lock()
	defer rm.mu.Unlock()

	ns := d.Nanoseconds()
	rm.fvcCallCount++
	rm.fvcTotalNs += ns
	if ns > rm.fvcMaxNs {
		rm.fvcMaxNs = ns
	}
	if cacheHit {
		rm.fvcCacheHits++
	} else {
		rm.fvcCacheMisses++
	}
}

// RecordSetContent records whether SetContent was called or skipped.
func (rm *RenderMetrics) RecordSetContent(called bool) {
	rm.mu.Lock()
	defer rm.mu.Unlock()
	if called {
		rm.setContentCalls++
	} else {
		rm.setContentSkips++
	}
}

// viewsPerSecond calculates redraw rate over the last 2 seconds of samples.
func (rm *RenderMetrics) viewsPerSecond() float64 {
	if len(rm.viewTimestamps) < 2 {
		return 0
	}
	// Use last 2 seconds of data
	cutoff := rm.viewTimestamps[len(rm.viewTimestamps)-1].Add(-2 * time.Second)
	count := 0
	for i := len(rm.viewTimestamps) - 1; i >= 0; i-- {
		if rm.viewTimestamps[i].Before(cutoff) {
			break
		}
		count++
	}
	if count < 2 {
		return float64(count)
	}
	window := rm.viewTimestamps[len(rm.viewTimestamps)-1].Sub(rm.viewTimestamps[len(rm.viewTimestamps)-count])
	if window <= 0 {
		return 0
	}
	return float64(count) / window.Seconds()
}

// percentile returns the p-th percentile (0-100) from sorted samples.
func (rm *RenderMetrics) percentile(p int) time.Duration {
	if len(rm.viewSamples) == 0 {
		return 0
	}
	sorted := make([]int64, len(rm.viewSamples))
	copy(sorted, rm.viewSamples)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i] < sorted[j] })
	idx := (p * len(sorted)) / 100
	if idx >= len(sorted) {
		idx = len(sorted) - 1
	}
	return time.Duration(sorted[idx])
}

// Report generates a human-readable diagnostic report.
func (rm *RenderMetrics) Report() string {
	rm.mu.Lock()
	defer rm.mu.Unlock()

	elapsed := time.Since(rm.sessionStart)
	var sb strings.Builder

	sb.WriteString("╔══════════════════════════════════════════════════════════╗\n")
	sb.WriteString("║           KYREX RENDER LOOP DIAGNOSTIC REPORT           ║\n")
	sb.WriteString("╚══════════════════════════════════════════════════════════╝\n\n")

	sb.WriteString(fmt.Sprintf("Session duration: %s\n\n", elapsed.Round(time.Second)))

	// ── View() timing ──
	sb.WriteString("─── View() Render Time ───────────────────────────────────\n")
	sb.WriteString(fmt.Sprintf("  Total calls:    %d\n", rm.viewCallCount))
	if rm.viewCallCount > 0 {
		avgNs := rm.viewTotalNs / rm.viewCallCount
		sb.WriteString(fmt.Sprintf("  Average:        %s\n", time.Duration(avgNs)))
		sb.WriteString(fmt.Sprintf("  Min:            %s\n", time.Duration(rm.viewMinNs)))
		sb.WriteString(fmt.Sprintf("  Max:            %s\n", time.Duration(rm.viewMaxNs)))
		sb.WriteString(fmt.Sprintf("  P50:            %s\n", rm.percentile(50)))
		sb.WriteString(fmt.Sprintf("  P90:            %s\n", rm.percentile(90)))
		sb.WriteString(fmt.Sprintf("  P99:            %s\n", rm.percentile(99)))
	}
	sb.WriteString("\n")

	// ── Redraw rate ──
	sb.WriteString("─── Redraw Rate ──────────────────────────────────────────\n")
	vps := rm.viewsPerSecond()
	sb.WriteString(fmt.Sprintf("  Current rate:   %.1f views/sec (last 2s window)\n", vps))
	if elapsed.Seconds() > 0 {
		overallRate := float64(rm.viewCallCount) / elapsed.Seconds()
		sb.WriteString(fmt.Sprintf("  Overall avg:    %.1f views/sec\n", overallRate))
	}
	sb.WriteString("\n")

	// ── Message frequency by type ──
	sb.WriteString("─── Message Frequency by Type ────────────────────────────\n")
	type msgEntry struct {
		name  string
		count int64
		dirty int64
	}
	var entries []msgEntry
	for k, v := range rm.msgCounts {
		entries = append(entries, msgEntry{k, v, rm.dirtyByMsgType[k]})
	}
	sort.Slice(entries, func(i, j int) bool { return entries[i].count > entries[j].count })

	var totalMsgs int64
	for _, e := range entries {
		totalMsgs += e.count
	}
	for _, e := range entries {
		pct := float64(0)
		if totalMsgs > 0 {
			pct = float64(e.count) * 100 / float64(totalMsgs)
		}
		dirtyStr := ""
		if e.dirty > 0 {
			dirtyStr = fmt.Sprintf(" (dirty: %d)", e.dirty)
		}
		sb.WriteString(fmt.Sprintf("  %-20s %6d  (%5.1f%%)%s\n", e.name, e.count, pct, dirtyStr))
	}
	sb.WriteString(fmt.Sprintf("  %-20s %6d\n", "TOTAL", totalMsgs))
	sb.WriteString("\n")

	// ── Dirty trigger breakdown ──
	sb.WriteString("─── Viewport Dirty Triggers ──────────────────────────────\n")
	sb.WriteString(fmt.Sprintf("  Total dirty events:  %d\n", rm.dirtyTriggers))
	var dirtyEntries []msgEntry
	for k, v := range rm.dirtyByMsgType {
		dirtyEntries = append(dirtyEntries, msgEntry{k, 0, v})
	}
	sort.Slice(dirtyEntries, func(i, j int) bool { return dirtyEntries[i].dirty > dirtyEntries[j].dirty })
	for _, e := range dirtyEntries {
		pct := float64(0)
		if rm.dirtyTriggers > 0 {
			pct = float64(e.dirty) * 100 / float64(rm.dirtyTriggers)
		}
		sb.WriteString(fmt.Sprintf("  %-20s %6d  (%5.1f%%)\n", e.name, e.dirty, pct))
	}
	sb.WriteString("\n")

	// ── FullViewportContent timing ──
	sb.WriteString("─── FullViewportContent() ────────────────────────────────\n")
	sb.WriteString(fmt.Sprintf("  Total calls:    %d\n", rm.fvcCallCount))
	if rm.fvcCallCount > 0 {
		avgNs := rm.fvcTotalNs / rm.fvcCallCount
		sb.WriteString(fmt.Sprintf("  Average:        %s\n", time.Duration(avgNs)))
		sb.WriteString(fmt.Sprintf("  Max:            %s\n", time.Duration(rm.fvcMaxNs)))
		sb.WriteString(fmt.Sprintf("  Cache hits:     %d\n", rm.fvcCacheHits))
		sb.WriteString(fmt.Sprintf("  Cache misses:   %d\n", rm.fvcCacheMisses))
	}
	sb.WriteString("\n")

	// ── SetContent tracking ──
	sb.WriteString("─── Viewport.SetContent() ────────────────────────────────\n")
	sb.WriteString(fmt.Sprintf("  Actual calls:   %d\n", rm.setContentCalls))
	sb.WriteString(fmt.Sprintf("  Skipped (same): %d\n", rm.setContentSkips))
	total := rm.setContentCalls + rm.setContentSkips
	if total > 0 {
		sb.WriteString(fmt.Sprintf("  Skip rate:      %.1f%%\n", float64(rm.setContentSkips)*100/float64(total)))
	}
	sb.WriteString("\n")

	// ── Bottleneck diagnosis ──
	sb.WriteString("─── Bottleneck Analysis ──────────────────────────────────\n")

	avgViewNs := int64(0)
	if rm.viewCallCount > 0 {
		avgViewNs = rm.viewTotalNs / rm.viewCallCount
	}
	avgView := time.Duration(avgViewNs)

	// Classification
	var bottleneck string
	if vps > 30 {
		bottleneck = "MESSAGE VOLUME — redraw rate exceeds 30fps, terminal cannot keep up\n"
		bottleneck += "  → Too many View() calls per second causes flickering\n"
		bottleneck += "  → Each View() produces a full terminal rewrite\n"
	} else if avgView > 16*time.Millisecond {
		bottleneck = "RENDER COST — View() takes >16ms, cannot sustain 60fps\n"
		bottleneck += fmt.Sprintf("  → Average View() = %s\n", avgView)
		bottleneck += "  → FullViewportContent() rebuild is too expensive\n"
	} else if rm.fvcCacheMisses > rm.fvcCacheHits*2 && rm.fvcCallCount > 10 {
		bottleneck = "CACHE INEFFICIENCY — stable history cache missing frequently\n"
		bottleneck += fmt.Sprintf("  → Hits: %d, Misses: %d\n", rm.fvcCacheHits, rm.fvcCacheMisses)
		bottleneck += "  → History is changing on every render (tokens appending)\n"
	} else {
		bottleneck = "TERMINAL OUTPUT — render cost is acceptable but terminal I/O is the bottleneck\n"
		bottleneck += fmt.Sprintf("  → %d View() calls produced full terminal rewrites\n", rm.setContentCalls)
		bottleneck += fmt.Sprintf("  → Average View() = %s (acceptable)\n", avgView)
	}
	sb.WriteString("  Diagnosis: " + bottleneck)
	sb.WriteString("\n")

	return sb.String()
}

// WriteReport writes the report to a file.
func (rm *RenderMetrics) WriteReport(path string) error {
	report := rm.Report()
	return os.WriteFile(path, []byte(report), 0644)
}

// WriteMetricsReport is a public wrapper so main.go can trigger a dump.
func (m Model) WriteMetricsReport(path string) {
	if m._metrics != nil {
		m._metrics.WriteReport(path)
	}
}
