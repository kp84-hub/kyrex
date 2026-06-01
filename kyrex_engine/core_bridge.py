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
except ImportError as e:
    sys.stderr.write(f"FATAL: Initialization failure: {str(e)}\n")
    print(json.dumps({"type": "error", "message": f"Initialization failure: {str(e)}"}))
    sys.exit(1)

# Canonical workspace path resolution
WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # project root

# ── VS Code active file bridge state ──
ACTIVE_FILE_PATH = None
ACTIVE_FILE_CONTENT = None

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
    """Threaded stdin reader to bypass asyncio selector issues with pipes."""
    sys.stderr.write("DEBUG: stdin_thread started\n")
    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                sys.stderr.write("DEBUG: stdin EOF reached\n")
                loop.call_soon_threadsafe(queue.put_nowait, None)
                break
            
            line = line.strip()
            if not line:
                continue
                
            sys.stderr.write(f"DEBUG: stdin read: {line[:100]}...\n")
            loop.call_soon_threadsafe(queue.put_nowait, line)
        except Exception as e:
            sys.stderr.write(f"DEBUG: stdin_thread error: {e}\n")
            break

async def listen_to_go(engine: PlaneExecute):
    """Loops and pipes raw stdin streams directly into the engine's chat loop."""
    queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    
    # Start the stdin reader in a background thread
    threading.Thread(target=stdin_thread, args=(queue, loop), daemon=True).start()

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
                    sys.stderr.write("DEBUG: Interrupt requested\n")
                    continue
                user_input = payload.get("content", payload.get("value", ""))
            else:
                user_input = str(payload)

            if user_input:
                # ── Fat payload: active file attached by VS Code ──
                active_file = payload.get("activeFile") if isinstance(payload, dict) else None
                if active_file:
                    af_path = active_file.get("path", "")
                    af_content = active_file.get("content", "")
                    if af_path:
                        note = (
                            f"\n\n[SYSTEM NOTE: The user currently has '{af_path}' open"
                            f" in their editor. Its content is:\n```\n{af_content}\n```\n"
                            f"Use this context to inform your response if relevant.]"
                        )
                        user_input = user_input + note

                sys.stderr.write(f"DEBUG: Calling engine.chat with: {user_input[:50]}...\n")
                res, reasoning = await engine.chat(user_input=user_input)
                sys.stderr.write(f"DEBUG: engine.chat finished. Res len: {len(res) if res else 0}\n")
                
                # If the engine returned an exception string, emit an explicit error message first
                if res and res.startswith("[!] EXCEPTION CAUGHT:"):
                    error_payload = {
                        "type": "error",
                        "content": res
                    }
                    sys.stdout.write(json.dumps(error_payload) + "\n")
                    sys.stdout.flush()

                # Emit chat_done to finalize the response in TUI history
                sys.stderr.write(f"DEBUG: Sending chat_done. Content len: {len(res) if res else 0}, Reasoning len: {len(reasoning) if reasoning else 0}\n")
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
            sys.stderr.write(f"DEBUG: Bridge loop error: {str(e)}\n")
            import traceback
            traceback.print_exc(file=sys.stderr)

async def main():
    # Force sys.stdout to flush on every print statement instantly
    sys.stdout.reconfigure(line_buffering=True)
    if "-p" not in sys.argv[1:]:
        sys.stderr.write("DEBUG: Bridge main starting\n")
    
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
    engine = PlaneExecute(config=cfg)
    sys.stderr.write(f"DEBUG: PlaneExecute initialized with model: {engine.model}\n")
    
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
    
    await listen_to_go(engine)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        sys.stderr.write(f"DEBUG: Bridge crash: {e}\n")
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
