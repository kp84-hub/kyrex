# Interrupt Implementation

## Overview
Implemented immediate interrupt handling that cancels ongoing LLM streaming and tool execution when the user presses Esc.

## Changes Made

### 1. Core Engine (`kyrex/core.py`)
- Added `InterruptedError` exception class
- Added `_interrupt_event` threading.Event to `PlaneExecute.__init__()`
- Added `interrupt()` method to set the event
- Added `_check_interrupt()` method that raises `InterruptedError` if event is set
- Modified `chat()` method:
  - Clears interrupt event at start of each turn
  - Checks interrupt before each LLM call in recursion loop
  - Checks interrupt before each tool execution
  - Catches `InterruptedError` and returns empty result cleanly
- Modified tool execution to poll interrupt event every 100ms instead of blocking on `thread.join()`

### 2. Provider Interface (`kyrex/providers/base.py`)
- Updated abstract `chat()` method signature to accept `interrupt_event` parameter

### 3. OpenAI Provider (`kyrex/providers/openai_.py`)
- Added `interrupt_event` parameter to `chat()` method
- Added interrupt check at top of streaming loop — breaks immediately if event is set

### 4. Anthropic Provider (`kyrex/providers/anthropic.py`)
- Added `interrupt_event` parameter to `chat()` method
- Passed interrupt_event to `_chat_stream()`
- Added interrupt check in streaming event loop — breaks immediately if event is set

### 5. Bridge (`core_bridge.py`)
- Modified `listen_to_go()` to track `current_task` (asyncio.Task)
- When interrupt message received:
  - Calls `engine.interrupt()` to set the threading.Event
  - Cancels the running `current_task` if one exists
- Wrapped `engine.chat()` in `asyncio.create_task()` for cancellation support
- Added `asyncio.CancelledError` handler to emit empty `chat_done` message

## Interrupt Flow

1. User presses Esc during streaming/tool execution
2. TUI sends `{"type": "interrupt"}` via stdin
3. Bridge receives message and:
   - Calls `engine.interrupt()` → sets `_interrupt_event`
   - Cancels the running asyncio task
4. Engine detects interrupt at next checkpoint:
   - Provider streaming loop breaks immediately
   - Tool execution polling loop breaks
   - Next recursion iteration raises `InterruptedError`
5. `chat()` catches `InterruptedError`, saves session, returns `("", "")`
6. Bridge emits `chat_done` with empty content
7. TUI returns to idle state

## Checkpoint Locations

Interrupt is checked at:
- Before each LLM API call (recursion loop)
- Before each tool execution
- Every 100ms during tool thread wait
- Every streaming chunk from LLM provider

## Latency

- Streaming interrupt: Immediate (checked on every chunk)
- Tool interrupt: ≤100ms (polling interval)
- Recursion interrupt: Immediate (checked at loop start)
