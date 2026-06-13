import re

with open('tui/update.go', 'r') as f:
    content = f.read()

# Replace FastTickMsg handler
old_fast = '''\tcase FastTickMsg:
\t\t// Only flush viewport during active engine responses to prevent flickering during typing
\t\t// Skip entirely if we\\'re idle (no reasoning, no current token, not thinking)
\t\tif m.Reasoning != "" || m.CurrToken != "" || m.IsThinking {
\t\t\tthrottle := 150 * time.Millisecond
\t\t\tif m.Reasoning != "" || m.CurrToken != "" {
\t\t\t\tthrottle = 50 * time.Millisecond
\t\t\t}
\t\t\tif m._viewportDirty && !m._tokenCoalescePending && time.Since(m._lastViewportFlush) > throttle {
\t\t\t\tnewContent := m.FullViewportContent(m.Viewport.Width)
\t\t\t\t// Only call SetContent if content actually changed (avoids full viewport recalc)
\t\t\t\tif newContent != m._lastSetContent {
\t\t\t\t\tm.Viewport.SetContent(newContent)
\t\t\t\t\tm._lastSetContent = newContent
\t\t\t\t\tif !m.ScrollLock {
\t\t\t\t\t\tm.Viewport.GotoBottom()
\t\t\t\t\t}
\t\t\t\t}
\t\t\t\tm._lastViewportFlush = time.Now()
\t\t\t\tm._viewportDirty = false
\t\t\t}
\t\t}
\t\t// Continuous auto-scroll during selection'''

new_fast = '''\tcase FastTickMsg:
\t\t// Only flush viewport during active engine responses to prevent flickering during typing
\t\t// Skip entirely if we\\'re idle (no reasoning, no current token, not thinking)
\t\tif m.Reasoning != "" || m.CurrToken != "" || m.IsThinking {
\t\t\tthrottle := 150 * time.Millisecond
\t\t\tif m.Reasoning != "" || m.CurrToken != "" {
\t\t\t\tthrottle = 50 * time.Millisecond
\t\t\t}
\t\t\tif m._viewportDirty && !m._tokenCoalescePending && time.Since(m._lastViewportFlush) > throttle {
\t\t\t\tm.flushViewport()
\t\t\t\tm._lastViewportFlush = time.Now()
\t\t\t}
\t\t}
\t\t// Continuous auto-scroll during selection'''

if old_fast in content:
    content = content.replace(old_fast, new_fast)
    print("Replaced FastTickMsg handler")
else:
    print("ERROR: Could not find FastTickMsg handler")
    exit(1)

# Replace TickMsg handler
old_tick = '''\tcase TickMsg:
\t\tif m.IsThinking {
\t\t\tm.Timer++
\t\t}
\t\t// Only flush viewport during active engine responses
\t\tif m.Reasoning != "" || m.CurrToken != "" || m.IsThinking {
\t\t\tthrottle := 150 * time.Millisecond
\t\t\tif m.Reasoning != "" || m.CurrToken != "" {
\t\t\t\tthrottle = 50 * time.Millisecond
\t\t\t}
\t\t\tif m._viewportDirty && time.Since(m._lastViewportFlush) > throttle {
\t\t\t\tnewContent := m.FullViewportContent(m.Viewport.Width)
\t\t\t\t// Only call SetContent if content actually changed (avoids full viewport recalc)
\t\t\t\tif newContent != m._lastSetContent {
\t\t\t\t\tm.Viewport.SetContent(newContent)
\t\t\t\t\tm._lastSetContent = newContent
\t\t\t\t\tif !m.ScrollLock {
\t\t\t\t\t\tm.Viewport.GotoBottom()
\t\t\t\t\t}
\t\t\t\t}
\t\t\t\tm._lastViewportFlush = time.Now()
\t\t\t\tm._viewportDirty = false
\t\t\t}
\t\t}
\t\tif m.Toast != "" && time.Now().After(m.ToastEnd) {'''

new_tick = '''\tcase TickMsg:
\t\tif m.IsThinking {
\t\t\tm.Timer++
\t\t}
\t\t// Only flush viewport during active engine responses
\t\tif m.Reasoning != "" || m.CurrToken != "" || m.IsThinking {
\t\t\tthrottle := 150 * time.Millisecond
\t\t\tif m.Reasoning != "" || m.CurrToken != "" {
\t\t\t\tthrottle = 50 * time.Millisecond
\t\t\t}
\t\t\tif m._viewportDirty && time.Since(m._lastViewportFlush) > throttle {
\t\t\t\tm.flushViewport()
\t\t\t\tm._lastViewportFlush = time.Now()
\t\t\t}
\t\t}
\t\tif m.Toast != "" && time.Now().After(m.ToastEnd) {'''

if old_tick in content:
    content = content.replace(old_tick, new_tick)
    print("Replaced TickMsg handler")
else:
    print("ERROR: Could not find TickMsg handler")
    exit(1)

# Replace TokenCoalesceMsg handler
old_coalesce = '''\tcase TokenCoalesceMsg:
\t\t// Immediate viewport flush after token/reasoning burst (16ms coalesce window)
\t\tm._tokenCoalescePending = false
\t\tif m._viewportDirty {
\t\t\tnewContent := m.FullViewportContent(m.Viewport.Width)
\t\t\tif newContent != m._lastSetContent {
\t\t\t\tm.Viewport.SetContent(newContent)
\t\t\t\tm._lastSetContent = newContent
\t\t\t\tif !m.ScrollLock {
\t\t\t\t\tm.Viewport.GotoBottom()
\t\t\t\t}
\t\t\t}
\t\t\tm._lastViewportFlush = time.Now()
\t\t\tm._viewportDirty = false
\t\t}'''

new_coalesce = '''\tcase TokenCoalesceMsg:
\t\t// Immediate viewport flush after token/reasoning burst (16ms coalesce window)
\t\tm._tokenCoalescePending = false
\t\tm.flushViewport()
\t\tm._lastViewportFlush = time.Now()'''

if old_coalesce in content:
    content = content.replace(old_coalesce, new_coalesce)
    print("Replaced TokenCoalesceMsg handler")
else:
    print("ERROR: Could not find TokenCoalesceMsg handler")
    exit(1)

with open('tui/update.go', 'w') as f:
    f.write(content)

print("Successfully updated tui/update.go")
