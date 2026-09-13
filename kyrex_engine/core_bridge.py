import sys
import os
import json
import time
import asyncio
import threading
from pathlib import Path
from kyrex.config import ConfigManager
from daemon_bridge import (
    TeeStdout,
    DaemonHub,
    is_daemon_mode,
    idle_exit_seconds,
)

# Fix package paths
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

try:
    from kyrex.core import PlaneExecute, _estimate_cost
    from kyrex.toolbox import _pending_edits, _edit_results, _pending_confirmations, _confirmation_results
except ImportError as e:
    sys.stderr.write(f"FATAL: Initialization failure: {str(e)}\n")
    print(json.dumps({"type": "error", "message": f"Initialization failure: {str(e)}"}))
    sys.exit(1)

# Canonical workspace path resolution — follows the WORKSPACE_ROOT env var set by the Go TUI
WORKSPACE_ROOT = os.environ.get("WORKSPACE_ROOT", os.getcwd())
# ── VS Code active file bridge state ──
ACTIVE_FILE_PATH = None
ACTIVE_FILE_CONTENT = None

# ── Real-time streaming usage stats state ──
# Accumulates content+reasoning characters for the current assistant turn and
# throttles silent sidebar updates so the TUI is not overwhelmed by JSON frames.
_streaming_usage_chars = 0
_streaming_usage_last_emit = 0.0
_streaming_usage_interval = 0.25  # seconds
_streaming_usage_lock = threading.Lock()

# ── Connection error detection ──
def _is_connection_error(e: Exception) -> bool:
    """Check if an exception is related to API/fetch connection failure."""
    msg = str(e).lower()
    keywords = [
        "connection", "timeout", "dns", "resolve", "econnrefused", "econnreset",
        "api key", "unauthorized", "401", "403", "authentication", "auth",
        "api_error", "rate_limit", "context_length_exceeded",
        "not found", "model not found", "server error", "502", "503",
    ]
    return any(k in msg for k in keywords)


def _friendly_connection_error(e: Exception) -> str:
    """Return a user-friendly message for connection/auth errors."""
    msg = str(e)
    low = msg.lower()
    if "api key" in low or "unauthorized" in low or "401" in low or "403" in low or "auth" in low:
        return (
            "Authentication failed. Your API key may be invalid or expired.\n"
            "  Run 'kx --setup' to reconfigure your credentials."
        )
    if "connection" in low or "timeout" in low or "dns" in low or "resolve" in low:
        return (
            "Could not reach the API server. Check your network connection\n"
            "  and base URL, then run 'kx --setup' to verify your configuration."
        )
    if "model" in low and ("not found" in low or "not support" in low):
        return (
            "The selected model is not available. Run 'kx --setup'\n"
            "  to pick a different model from the available list."
        )
    return f"API error: {msg[:100]}\n  Run 'kx --setup' to review and fix your configuration."


def _supports_unicode() -> bool:
    """Detect if the terminal supports Unicode/UTF-8 rendering."""
    # Check stdout encoding
    enc = getattr(sys.stdout, "encoding", "") or ""
    if enc.lower() in ("utf-8", "utf8", "unicode"):
        return True

    # Check TERM for known UTF-8-capable terminals
    term = os.environ.get("TERM", "")
    if term and ("256color" in term or "xterm" in term or "tmux" in term or "screen" in term or "kitty" in term or "alacritty" in term or "foot" in term or "wezterm" in term):
        return True

    # Windows Terminal or modern Windows host
    if os.environ.get("WT_SESSION"):
        return True

    return False


def _print_welcome_and_exit():
    """Print branded welcome screen with setup instructions and exit."""
    C = '\033[96m'
    W = '\033[97m'
    B = '\033[1m'
    N = '\033[0m'
    print()

    if _supports_unicode():
        # ── Full Unicode banner with box-drawing and block chars ──
        print(f"  {C}╭──────────────────────────────────────────────╮{N}")
        print(f"  {C}│{W}  ██╗  ██╗██╗   ██╗██████╗ ███████╗██╗  ██╗ {C}│{N}")
        print(f"  {C}│{W}  ██║ ██╔╝╚██╗ ██╔╝██╔══██╗██╔════╝╚██╗██╔╝ {C}│{N}")
        print(f"  {C}│{W}  █████╔╝  ╚████╔╝ ██████╔╝█████╗   ╚███╔╝  {C}│{N}")
        print(f"  {C}│{W}  ██╔═██╗   ╚██╔╝  ██╔══██╗██╔══╝   ██╔██╗  {C}│{N}")
        print(f"  {C}│{W}  ██║  ██╗   ██║   ██║  ██║███████╗██╔╝ ██╗ {C}│{N}")
        print(f"  {C}│{W}  ╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝ {C}│{N}")
        print(f"  {C}╰──────────────────────────────────────────────╯{N}")
        print(f"  {W}{B}            Terminal AI Agent{N}")
        print(f"  {C}──────────────────────────────────────────────────{N}")
        bullet = "\u2022"
    else:
        # ── Pure ASCII banner (no Unicode) ──
        print(f"  {C}+------------------------------------------------+{N}")
        print(f"  {C}|{W}                                                {C}|{N}")
        print(f"  {C}|{W}          K   Y   R   E   X                     {C}|{N}")
        print(f"  {C}|{W}          Terminal AI Agent                      {C}|{N}")
        print(f"  {C}|{W}                                                {C}|{N}")
        print(f"  {C}+------------------------------------------------+{N}")
        bullet = "-"

    print()
    print(f"  {W}Kyrex needs to be configured before first use.{N}")
    print(f"  {W}Run the setup wizard to connect to an AI provider:{N}")
    print()
    print(f"    {C}kx --setup{N}")
    print()
    print(f"  {W}The wizard will guide you through:{N}")
    print(f"  {W}  {bullet} Choosing a provider (OpenAI-compatible or Anthropic){N}")
    print(f"  {W}  {bullet} Setting your API key or environment variable{N}")
    print(f"  {W}  {bullet} Selecting a model from available options{N}")
    print(f"  {W}  {bullet} Testing the connection{N}")
    print()
    sys.exit(0)


def gather_workspace_files():
    """Return structured workspace layout: top-level dirs and key files only."""
    ignored_dirs = {".git", "node_modules", "venv", ".venv", "__pycache__",
                    "dist", "build", ".px_sessions", "kyrex-engine", ".kyrex_sessions",
                    ".aider.tags.cache.v4", ".claude", ".opencode", ".states"}
    key_file_names = {"main.py", "app.py", "index.py", "package.json",
                      "go.mod", "requirements.txt", "Procfile", "README.md"}
    dirs = []
    files = []
    try:
        root_path = Path(os.getcwd())
        for p in root_path.iterdir():
            name = p.name
            if p.is_dir():
                if name not in ignored_dirs and not name.endswith(".egg-info"):
                    dirs.append(name)
            elif p.is_file():
                if name in key_file_names:
                    files.append(name)
    except Exception as e:
        sys.stderr.write(f"gather_workspace_files error: {e}\n")
    dirs.sort()
    files.sort()
    return {"dirs": dirs[:10], "files": files[:5]}

def _emit_streaming_usage_stats(engine: PlaneExecute, streaming_completion_chars: int = 0):
    """Emit a silent usage-stats frame for live sidebar updates during streaming.

    The engine's official totals are only finalized after the provider call ends,
    so we patch in a running completion-token estimate (~4 chars per token) while
    the response is still streaming. The sidebar receives this as a
    "usage_stats_silent" tui_pause frame and updates without opening an overlay.
    """
    stats = engine.get_usage_stats()
    if streaming_completion_chars:
        extra_completion = streaming_completion_chars // 4
        stats["completion_tokens"] = stats.get("completion_tokens", 0) + extra_completion
        stats["cost"] = _estimate_cost(
            engine.model,
            stats.get("prompt_tokens", 0),
            stats.get("completion_tokens", 0),
        )
    sys.stdout.write(json.dumps({
        "type": "tui_pause",
        "value": "usage_stats_silent",
        "files": stats,
    }) + "\n")
    sys.stdout.flush()


def _dispatch_engine_line(line, queue, loop, engine) -> bool:
    """Intercept control messages (interrupt, edit_decision, confirm_response)
    directly from a reader thread.

    - The async chat loop blocks on Event.wait() during a propose_edit or
      _propose_deletion, so edit_decision and confirm_response are resolved
      immediately from the reader thread.
    - Interrupts are applied directly to the engine so they cancel the
      active turn even while the main loop is awaiting engine.chat().

    Returns True if the line was consumed here (do not queue it).
    Shared by the stdin reader thread and the daemon socket client thread.
    """
    try:
        payload = json.loads(line)
        if isinstance(payload, dict):
            if payload.get("type") == "interrupt":
                engine.interrupt()
                return True  # Don't queue — already applied
            if payload.get("type") == "edit_decision":
                edit_id = payload.get("editId", "")
                accepted = payload.get("accepted", False)
                _edit_results[edit_id] = accepted
                if edit_id in _pending_edits:
                    _pending_edits[edit_id].set()
                return True  # Don't push to queue — already handled
            if payload.get("type") == "confirm_response":
                confirm_id = payload.get("id", "")
                approved = payload.get("approved", False)
                _confirmation_results[confirm_id] = approved
                if confirm_id in _pending_confirmations:
                    _pending_confirmations[confirm_id].set()
                return True  # Don't push to queue — already handled
    except (json.JSONDecodeError, KeyError):
        pass  # Not a JSON control message, pass through normally
    return False


def stdin_thread(queue, loop, engine, shutdown_event):
    """Threaded stdin reader to bypass asyncio selector issues with pipes.

    Intercepts control messages via _dispatch_engine_line (shared with the
    daemon socket reader). Checks shutdown_event on every iteration for
    clean teardown.
    """
    while not shutdown_event.is_set():
        try:
            # Use select-like read with timeout so shutdown isn't blocked
            # on stdin.readline() hanging when stdin is a pipe
            import select
            if hasattr(sys.stdin, 'fileno') and sys.platform != "win32":
                readable, _, _ = select.select([sys.stdin], [], [], 0.5)
                if not readable:
                    continue
                if shutdown_event.is_set():
                    break
            
            line = sys.stdin.readline()
            if not line:
                loop.call_soon_threadsafe(queue.put_nowait, None)
                break
            
            line = line.strip()
            if not line:
                continue

            if _dispatch_engine_line(line, queue, loop, engine):
                continue
            
            loop.call_soon_threadsafe(queue.put_nowait, line)
        except Exception:
            break

def _drain_queue(queue: asyncio.Queue):
    """Discard any pending queued input, preserving EOF sentinel if present."""
    eof_seen = False
    while not queue.empty():
        try:
            item = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        if item is None:
            eof_seen = True
    if eof_seen:
        queue.put_nowait(None)


async def _wait_for_turn(chat_task: asyncio.Task, engine: PlaneExecute):
    """Await an engine.chat() turn while watching for the interrupt signal."""
    interrupted = False
    while not chat_task.done():
        if engine._interrupt_event.is_set() or engine._interrupted_this_turn:
            interrupted = True
            chat_task.cancel()
            break
        await asyncio.sleep(0.05)

    if not chat_task.done():
        try:
            await chat_task
        except asyncio.CancelledError:
            pass

    interrupted = interrupted or engine._interrupted_this_turn

    if chat_task.done() and not chat_task.cancelled():
        return chat_task.result(), interrupted
    return ("", ""), interrupted


async def listen_to_go(engine: PlaneExecute, hub: "DaemonHub | None" = None):
    """Loops and pipes raw input lines into the engine's chat loop.

    Input source: stdin reader thread by default; when `hub` is given (daemon
    mode), connected UI sockets feed the same queue instead and the hub's
    turn_active flag tracks whether a turn is in flight (used for idle exit).
    """
    queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    
    # Shutdown signal for the stdin reader thread
    shutdown_event = threading.Event()
    
    if hub is not None:
        # Daemon mode: hub threads feed the queue, stdin is unused (the
        # spawner detaches the process with no controlling terminal).
        hub.attach(queue, loop)
    else:
        # Start the stdin reader in a background thread
        stdin_thread_ref = threading.Thread(target=stdin_thread, args=(queue, loop, engine, shutdown_event), daemon=True)
        stdin_thread_ref.start()

    # Track the currently running chat task so interrupt can cancel it
    current_task: asyncio.Task | None = None

    while True:
        line = await queue.get()
        if line is None:
            break
        
        try:
            payload = json.loads(line)
            user_input = ""
            if isinstance(payload, dict):
                p_type = payload.get("type")

                if p_type == "interrupt":
                    # This handler is a safety net — normally stdin_thread
                    # already intercepts interrupts and calls engine.interrupt()
                    # directly. If one does reach the queue, apply it but do
                    # NOT drain the queue: the user's next prompt must survive.
                    engine.interrupt()
                    if current_task is not None and not current_task.done():
                        current_task.cancel()
                    engine._interrupted_this_turn = False
                    continue
                user_input = payload.get("content", payload.get("value", ""))
            else:
                user_input = str(payload)

           # ── Fat payload: active file attached by VS Code ──
            active_file = payload.get("activeFile") if isinstance(payload, dict) else None
            if active_file:
                af_path = active_file.get("path", "")
                af_content = active_file.get("content", "")
                if af_path:
                    engine.session.append({
                        "role": "system",
                        "content": (
                            f"ACTIVE FILE CONTEXT: The user currently has '{af_path}' open in their editor.\n"
                            f"```\n{af_content}\n```\n"
                            f"This file is available for reading, editing, or reasoning about."
                        )
                    })

            if user_input:
                # Reset live usage counters for the new turn so sidebar updates
                # start from the current baseline as soon as tokens arrive.
                global _streaming_usage_chars, _streaming_usage_last_emit
                with _streaming_usage_lock:
                    _streaming_usage_chars = 0
                    _streaming_usage_last_emit = 0.0
                if hub is not None:
                    hub.turn_active = True
                current_task = asyncio.create_task(engine.chat(user_input=user_input))
                try:
                    chat_result, turn_interrupted = await _wait_for_turn(current_task, engine)
                except asyncio.CancelledError:
                    # Interrupt cancelled the task — emit chat_done with empty content
                    chat_result = ("", "")
                    turn_interrupted = True
                finally:
                    if hub is not None:
                        hub.turn_active = False
                current_task = None
                if turn_interrupted or engine._interrupted_this_turn:
                    # Reset the interrupt flag but do NOT drain the queue.
                    # The user may have already typed a new redirect prompt
                    # while the engine was cancelling — that message must
                    # be processed, not thrown away.
                    engine._interrupted_this_turn = False

                # Guard: engine.chat() must always return a (str, str) tuple
                if chat_result is None or not isinstance(chat_result, tuple) or len(chat_result) != 2:
                    chat_result = ("", "")
                res, reasoning = chat_result
                res = res or ""
                reasoning = reasoning or ""

                if res and res.startswith("[!] EXCEPTION CAUGHT:"):
                    error_payload = {
                        "type": "error",
                        "content": res
                    }
                    sys.stdout.write(json.dumps(error_payload) + "\n")
                    sys.stdout.flush()

                # Emit chat_done to finalize the response in TUI history
                chat_done_payload = {
                    "type": "chat_done",
                    "content": res or "",
                    "reasoning": reasoning or ""
                }
                sys.stdout.write(json.dumps(chat_done_payload) + "\n")
                sys.stdout.flush()

                # Auto-push usage stats after every completed turn (silent — no overlay)
                stats = engine.get_usage_stats()
                sys.stdout.write(json.dumps({
                    "type": "tui_pause",
                    "value": "usage_stats_silent",
                    "files": stats,
                }) + "\n")
                sys.stdout.flush()
                
                # Push a state sync refresh frame after the run turns finish
                status_payload = {
                    "type": "phase",
                    "value": "IDLE",
                    "model": getattr(engine, "model", None),
                    "provider": getattr(engine.provider, "name", "Unknown") if hasattr(engine.provider, "name") else "Unknown",
                    "context": f"Workspace: {os.getcwd()}",
                    "files": gather_workspace_files()
                }
                sys.stdout.write(json.dumps(status_payload) + "\n")
                sys.stdout.flush()

                # Usage stats are auto-pushed after every completed turn and
                # can also be requested explicitly with /usage. A compact
                # token/cost tracker is shown in the sidebar.
        except Exception as e:
            if _is_connection_error(e):
                friendly = _friendly_connection_error(e)
                error_payload = {"type": "error", "content": friendly}
                sys.stdout.write(json.dumps(error_payload) + "\n")
                sys.stdout.flush()
            else:
                import traceback
                traceback.print_exc(file=sys.stderr)

async def main():
    # Force sys.stdout to flush on every print statement instantly
    sys.stdout.reconfigure(line_buffering=True)
    args = sys.argv[1:]

    # ── Daemon mode (background engine) ──────────────────────────────────
    # The engine outlives the UI: it hosts a local TCP socket, buffers every
    # emitted line for replay, auto-resolves approval gates while detached,
    # and exits after a configurable idle window. See daemon_bridge.py.
    # The tee goes first so even engine-init output is captured for replay;
    # bind() happens after init below, so a crashed init never leaves a
    # stale control file behind.
    daemon_hub: "DaemonHub | None" = None
    if is_daemon_mode() and "-p" not in args:
        daemon_hub = DaemonHub(WORKSPACE_ROOT, idle_exit=idle_exit_seconds())
        sys.stdout = TeeStdout(sys.stdout, daemon_hub)

    # One-shot print mode: kx -p "prompt" [--json]
    if "-p" in args:
        cfg = ConfigManager()  # resolves project config, then global
        cfg.load()
        engine = PlaneExecute(config=cfg)
        idx = args.index("-p")
        prompt_parts = [a for a in args[idx+1:] if a != "--json"]
        prompt = " ".join(prompt_parts)
        mode = "json" if "--json" in args else "text"
        # In print mode stream directly to stdout, suppress final print duplication
        engine._stream_handler = lambda chunk: sys.stdout.write(chunk) if chunk else None
        engine._reasoning_handler = None
        result, _ = await engine.chat(prompt)
        if mode == "json":
            import json as _json
            print(_json.dumps({"status": "ok" if result else "empty", "result": result}))
        # streaming already printed in text mode
        return

    if "--setup" in args:
        cfg = ConfigManager()
        cfg.setup_wizard()
        return

    # Initialize your core state machine engine
    cfg = ConfigManager()  # resolves project config, then global
    cfg.load()
    try:
        engine = PlaneExecute(config=cfg)
    except Exception as e:
        if _is_connection_error(e):
            sys.stderr.write(_friendly_connection_error(e) + "\n")
        else:
            sys.stderr.write(f"FATAL: Engine initialization failed: {e}\n")
        sys.exit(1)
    # Map TUI JSON streamers directly into core callbacks
    def stream_token(chunk):
        global _streaming_usage_chars, _streaming_usage_last_emit
        if chunk:
            msg = json.dumps({"type": "token", "content": chunk})
            sys.stdout.write(msg + "\n")
            sys.stdout.flush()
            with _streaming_usage_lock:
                _streaming_usage_chars += len(chunk)
                now = time.monotonic()
                if now - _streaming_usage_last_emit >= _streaming_usage_interval:
                    _streaming_usage_last_emit = now
                    _emit_streaming_usage_stats(engine, _streaming_usage_chars)

    def stream_reasoning(chunk):
        global _streaming_usage_chars, _streaming_usage_last_emit
        if chunk:
            msg = json.dumps({"type": "reasoning", "content": chunk})
            sys.stdout.write(msg + "\n")
            sys.stdout.flush()
            with _streaming_usage_lock:
                _streaming_usage_chars += len(chunk)
                now = time.monotonic()
                if now - _streaming_usage_last_emit >= _streaming_usage_interval:
                    _streaming_usage_last_emit = now
                    _emit_streaming_usage_stats(engine, _streaming_usage_chars)

    engine._stream_handler = stream_token
    engine._reasoning_handler = stream_reasoning

    # Wire tool telemetry
    def on_tool_start(name, args):
        msg = json.dumps({"type": "tool_start", "name": name, "args": args})
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()

    def on_tool_result(name, result):
        msg = json.dumps({"type": "tool_result", "name": name, "result": result})
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()

    engine._on_tool_start = on_tool_start
    engine._on_tool_result = on_tool_result

    # Progressive final-round detection callback
    def on_final_round_signal(signal_type):
        """Emits final_round_starting or round_has_tools_after_all JSON messages."""
        msg = json.dumps({"type": signal_type})
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()

    engine._final_round_handler = on_final_round_signal

    # Emit session_state so Go populates model/workspace/files
    session_payload = {
        "type": "session_state",
        "model": getattr(engine, "model", None),
        "provider": getattr(engine.provider, "name", "Unknown") if hasattr(engine.provider, "name") else "Unknown",
        "context": os.getcwd(),
        "files": gather_workspace_files()
    }
    sys.stdout.write(json.dumps(session_payload) + "\n")
    sys.stdout.flush()
    
    # Then emit phase to set IDLE state
    phase_payload = {"type": "phase", "value": "IDLE"}
    sys.stdout.write(json.dumps(phase_payload) + "\n")
    sys.stdout.flush()
    
    if daemon_hub is not None:
        # Engine is live: publish the control file so the IDE can attach,
        # and start the accept/replay/broadcast loop.
        daemon_hub.bind()
        serve_thread = threading.Thread(target=daemon_hub.serve, daemon=True)
        serve_thread.start()
        daemon_hub.branch_provider = lambda: getattr(
            getattr(engine, "session", None), "current_branch_name", None)
        import signal as _signal

        def _graceful_sigterm(_sig, _frm):
            daemon_hub.request_stop()

        try:
            _signal.signal(_signal.SIGTERM, _graceful_sigterm)
        except Exception:
            pass
        sys.stderr.write(
            f"[daemon] listening on 127.0.0.1:{daemon_hub.port} "
            f"(workspace: {daemon_hub.workspace})\n"
        )

    try:
        await listen_to_go(engine, hub=daemon_hub)
        if daemon_hub is not None:
            # Graceful daemon exit (idle window, shutdown message, SIGTERM):
            # persist the final session state before leaving.
            try:
                engine.session.save()
            except Exception:
                pass
    except Exception as e:
        if _is_connection_error(e):
            sys.stderr.write(_friendly_connection_error(e) + "\n")
        else:
            sys.stderr.write(f"FATAL: Bridge runtime error: {e}\n")
            import traceback
            traceback.print_exc(file=sys.stderr)
        sys.exit(1)
    finally:
        if daemon_hub is not None:
            daemon_hub.cleanup()

def _run_main():
    """Run the main async entry point with error handling."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        if _is_connection_error(e):
            sys.stderr.write(_friendly_connection_error(e) + "\n")
        else:
            import traceback
            traceback.print_exc(file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    # ── Standalone wizard step: bypass all startup, read JSON from stdin, print result, exit ──
    if "--wizard-step" in sys.argv:
        """Reads one JSON line from stdin, calls ConfigManager method, prints JSON result, exits."""
        try:
            line = sys.stdin.readline()
            if not line:
                print(json.dumps({"success": False, "message": "No input received"}))
                sys.exit(0)
            step_request = json.loads(line.strip())
            action = step_request.get("action", "")
            provider = step_request.get("provider", "openai")
            api_key = step_request.get("api_key", "")
            base_url = step_request.get("base_url", "")
            model = step_request.get("model", "")

            # Use an ephemeral config populated only from stdin values
            cfg = ConfigManager()
            cfg._data = {
                "provider": provider,
                "api_key": api_key,
                "base_url": base_url,
                "model": model,
            }

            if action == "test_connection":
                ok, msg = cfg.test_connection()
                print(json.dumps({"success": ok, "message": msg}))
                sys.exit(0)
            elif action == "list_models":
                models = cfg._fetch_model_list(provider, api_key, base_url)
                if models is not None:
                    print(json.dumps({"success": True, "models": models}))
                else:
                    print(json.dumps({"success": False, "models": None}))
                sys.exit(0)
            else:
                print(json.dumps({"success": False, "message": f"Unknown action: {action}"}))
                sys.exit(0)
        except Exception as e:
            print(json.dumps({"success": False, "message": f"Wizard step error: {str(e)}"}))
            sys.exit(1)

    # ── Bypass config check for VS Code or flag-only modes ──
    # VS Code extension handles its own config; skip the welcome screen
    if os.environ.get("KYREX_VSCODE") == "1" or "--setup" in sys.argv or "-p" in sys.argv:
        _run_main()
    else:
        # ── Normal startup: config check before TUI/async init ──
        _cfg = ConfigManager()  # resolves project config, then global
        _cfg_path = _cfg.config_path
        _config_exists = _cfg_path.exists()
        _cfg.load()

        _has_local_key = False
        if _config_exists:
            _d = _cfg._data
            _has_local_key = bool(
                _d.get("api_key") or _d.get("api_key_env")
                or _d.get("openai_api_key") or _d.get("anthropic_api_key")
            )

        if not _config_exists or not _has_local_key:
            _print_welcome_and_exit()

        _run_main()
