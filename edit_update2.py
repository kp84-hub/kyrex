import re

with open('tui/update.go', 'r') as f:
    content = f.read()

# Use regex to find and replace the three handlers

# 1. FastTickMsg handler
fast_pattern = r'(\tcase FastTickMsg:\n\t\t// Only flush viewport during active engine responses.*?\tif m\._viewportDirty && !m\._tokenCoalescePending && time\.Since\(m\._lastViewportFlush\) > throttle \{\n)(\t\t\t\tnewContent := m\.FullViewportContent\(m\.Viewport\.Width\)\n\t\t\t\t// Only call SetContent if content actually changed.*?\t\t\t\t\}\n\t\t\t\tm\._lastViewportFlush = time\.Now\(\)\n\t\t\t\tm\._viewportDirty = false\n\t\t\t\})'

fast_replacement = r'''\1\t\t\t\tm.flushViewport()\n\t\t\t\tm._lastViewportFlush = time.Now()\n\t\t\t}'''

new_content, fast_count = re.subn(fast_pattern, fast_replacement, content, count=1, flags=re.DOTALL)
if fast_count == 0:
    print("ERROR: Could not find FastTickMsg handler")
    exit(1)
print(f"Replaced FastTickMsg handler")

# 2. TickMsg handler
tick_pattern = r'(\tcase TickMsg:\n\t\tif m\.IsThinking \{\n\t\t\tm\.Timer\+\+\n\t\t\}\n\t\t// Only flush viewport during active engine responses.*?\tif m\._viewportDirty && time\.Since\(m\._lastViewportFlush\) > throttle \{\n)(\t\t\t\tnewContent := m\.FullViewportContent\(m\.Viewport\.Width\)\n\t\t\t\t// Only call SetContent if content actually changed.*?\t\t\t\t\}\n\t\t\t\tm\._lastViewportFlush = time\.Now\(\)\n\t\t\t\tm\._viewportDirty = false\n\t\t\t\})'

tick_replacement = r'''\1\t\t\t\tm.flushViewport()\n\t\t\t\tm._lastViewportFlush = time.Now()\n\t\t\t}'''

new_content, tick_count = re.subn(tick_pattern, tick_replacement, new_content, count=1, flags=re.DOTALL)
if tick_count == 0:
    print("ERROR: Could not find TickMsg handler")
    exit(1)
print(f"Replaced TickMsg handler")

# 3. TokenCoalesceMsg handler
coalesce_pattern = r'\tcase TokenCoalesceMsg:\n\t\t// Immediate viewport flush after token/reasoning burst \(16ms coalesce window\)\n\t\tm\._tokenCoalescePending = false\n\t\tif m\._viewportDirty \{\n\t\t\tnewContent := m\.FullViewportContent\(m\.Viewport\.Width\)\n\t\t\tif newContent != m\._lastSetContent \{\n\t\t\t\tm\.Viewport\.SetContent\(newContent\)\n\t\t\t\tm\._lastSetContent = newContent\n\t\t\t\tif !m\.ScrollLock \{\n\t\t\t\t\tm\.Viewport\.GotoBottom\(\)\n\t\t\t\t\}\n\t\t\t\}\n\t\t\tm\._lastViewportFlush = time\.Now\(\)\n\t\t\tm\._viewportDirty = false\n\t\t\}'

coalesce_replacement = '''\tcase TokenCoalesceMsg:\n\t\t// Immediate viewport flush after token/reasoning burst (16ms coalesce window)\n\t\tm._tokenCoalescePending = false\n\t\tm.flushViewport()\n\t\tm._lastViewportFlush = time.Now()'''

new_content, coalesce_count = re.subn(coalesce_pattern, coalesce_replacement, new_content, count=1)
if coalesce_count == 0:
    print("ERROR: Could not find TokenCoalesceMsg handler")
    exit(1)
print(f"Replaced TokenCoalesceMsg handler")

with open('tui/update.go', 'w') as f:
    f.write(new_content)

print("Successfully updated tui/update.go")
