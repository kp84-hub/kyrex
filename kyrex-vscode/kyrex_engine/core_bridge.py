import sys
import os
import json
import time
import asyncio
import threading
from pathlib import Path

# Fix package paths
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

try:
    from kyrex.core import PlaneExecute
    from kyrex.toolbox import _pending_edits, _edit_results
except ImportError as e:
    sys.stderr.write(f"FATAL: Initialization failure: {str(e)}\n")
    print(json.dumps({"type": "error", "message": f"Initialization failure: {str(e)}"}))
    sys.exit(1)

# Canonical workspace path resolution
WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # project root

# ── VS Code active file bridge state ──
ACTIVE_FILE_PATH = None
ACTIVE_FILE_CONTENT = None

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

def stdin_thread(queue, loop):
    """Threaded stdin reader to bypass asyncio selector issues with pipes.
    
    Intercepts edit_decision messages directly to prevent deadlock:
    The async chat loop blocks on Event.wait() during a propose_edit,
    so it cannot read stdin. This thread resolves the edit immediately
    by signaling the waiting Event, then continues reading stdin.
    """
    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                loop.call_soon_threadsafe(queue.put_nowait, None)
                break
            
            line = line.strip()
            if not line:
                continue
                
            # ── Intercept edit_decision to prevent deadlock ──
            # The chat loop is blocked on Event.wait() for a propose_edit.
            # If we push this to the queue, listen_to_go can't read it
            # because it's stuck awaiting engine.chat(). Deadlock.
            # Instead, resolve the edit directly from this thread.
            try:
                payload = json.loads(line)
                if isinstance(payload, dict) and payload.get("type") == "edit_decision":
                    edit_id = payload.get("editId", "")
                    accepted = payload.get("accepted", False)
                    _edit_results[edit_id] = accepted
                    if edit_id in _pending_edits:
                        _pending_edits[edit_id].set()
                    continue  # Don't push to queue — already handled
            except (json.JSONDecodeError, KeyError):
                pass  # Not a JSON edit_decision, pass through normally
            
            loop.call_soon_threadsafe(queue.put_nowait, line)
        except Exception:
            break

async def listen_to_go(engine: PlaneExecute):
    """Loops and pipes raw stdin streams directly into the engine's chat loop."""
    queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    
    # Start the stdin reader in a background thread
    threading.Thread(target=stdin_thread, args=(queue, loop), daemon=True).start()

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
                    # Signal the engine to stop mid-tool / mid-stream
                    engine.interrupt()
                    # Cancel the running chat task if one exists
                    if current_task is not None and not current_task.done():
                        current_task.cancel()
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
                current_task = asyncio.create_task(engine.chat(user_input=user_input))
                try:
                    chat_result = await current_task
                except asyncio.CancelledError:
                    # Interrupt cancelled the task — emit chat_done with empty content
                    chat_result = ("", "")
                current_task = None

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

    # One-shot print mode: kx -p "prompt" [--json]
    if "-p" in args:
        from kyrex.config import ConfigManager
        cfg = ConfigManager(Path(WORKSPACE_ROOT) / ".px" / "config.json")
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
        from kyrex.config import ConfigManager
        cfg = ConfigManager()
        cfg.setup_wizard()
        return

    # Initialize your core state machine engine
    from kyrex.config import ConfigManager
    cfg = ConfigManager(Path(WORKSPACE_ROOT) / ".px" / "config.json")
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
        if chunk:
            msg = json.dumps({"type": "token", "content": chunk})
            sys.stdout.write(msg + "\n")
            sys.stdout.flush()

    def stream_reasoning(chunk):
        if chunk:
            msg = json.dumps({"type": "reasoning", "content": chunk})
            sys.stdout.write(msg + "\n")
            sys.stdout.flush()

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

    # Emit session_state so Go populates model/workspace/files
    session_payload = {
        "type": "session_state",
        "model": getattr(engine, "model", None),
        "provider": getattr(engine.provider, "name", "Unknown") if hasattr(engine.provider, "name") else "Unknown",
        "mode": getattr(engine, "mode", "plan"),
        "context": os.getcwd(),
        "files": gather_workspace_files()
    }
    sys.stdout.write(json.dumps(session_payload) + "\n")
    sys.stdout.flush()
    
    # Then emit phase to set IDLE state
    phase_payload = {"type": "phase", "value": "IDLE"}
    sys.stdout.write(json.dumps(phase_payload) + "\n")
    sys.stdout.flush()
    
    try:
        await listen_to_go(engine)
    except Exception as e:
        if _is_connection_error(e):
            sys.stderr.write(_friendly_connection_error(e) + "\n")
        else:
            sys.stderr.write(f"FATAL: Bridge runtime error: {e}\n")
            import traceback
            traceback.print_exc(file=sys.stderr)
        sys.exit(1)

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
    # ── Bypass config check for VS Code or flag-only modes ──
    # VS Code extension handles its own config; skip the welcome screen
    if os.environ.get("KYREX_VSCODE") == "1" or "--setup" in sys.argv or "-p" in sys.argv:
        _run_main()
    else:
        # ── Normal startup: config check before TUI/async init ──
        from kyrex.config import ConfigManager
        _cfg = ConfigManager(Path(WORKSPACE_ROOT) / ".px" / "config.json")
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
