import os
import json
import sys
import time
import inspect
import threading
import asyncio
from pathlib import Path
from threading import Timer
from .providers import get_provider, BaseProvider
from .extensions import registry as ext_registry
from .session import TreeSessionManager
from .skills import SkillsLoader
from .tools import MCPManager
from .audit import ReasoningAuditLogger
from .toolbox import ToolBox, BUILTIN_TOOLS, _is_interactive


_MCP_CONNECTOR_REQUIRED_KEYS = {
    "id", "name", "description", "command", "args", "requirements",
    "auth", "source_url", "verification",
}
_MCP_AUTH_KEYS = {"mode", "warning"}
_MCP_VERIFICATION_KEYS = {"status", "checked_at"}
_MCP_AUTH_MODES = {"none", "environment_variable", "browser_sign_in", "manual_setup"}


def _load_mcp_connector_manifest():
    """Load and validate the bundled MCP connector manifest."""
    manifest_path = Path(__file__).with_name("assets") / "mcp-connectors.json"
    with manifest_path.open(encoding="utf-8") as manifest_file:
        document = json.load(manifest_file)

    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("MCP connector manifest has an unsupported schema")
    connectors = document.get("connectors")
    if not isinstance(connectors, list):
        raise ValueError("MCP connector manifest connectors must be a list")

    for connector in connectors:
        if not isinstance(connector, dict) or set(connector) != _MCP_CONNECTOR_REQUIRED_KEYS:
            raise ValueError("MCP connector manifest contains an invalid connector")
        for key in ("id", "name", "description", "command", "source_url"):
            if not isinstance(connector[key], str) or not connector[key]:
                raise ValueError(f"MCP connector {key} must be a non-empty string")
        if not isinstance(connector["args"], list) or not all(isinstance(arg, str) for arg in connector["args"]):
            raise ValueError("MCP connector args must be a list of strings")
        if not isinstance(connector["requirements"], list) or not all(isinstance(req, str) for req in connector["requirements"]):
            raise ValueError("MCP connector requirements must be a list of strings")
        auth = connector["auth"]
        if (not isinstance(auth, dict) or set(auth) != _MCP_AUTH_KEYS
                or auth["mode"] not in _MCP_AUTH_MODES
                or not isinstance(auth["warning"], str) or not auth["warning"]):
            raise ValueError("MCP connector auth is invalid")
        verification = connector["verification"]
        if (not isinstance(verification, dict) or set(verification) != _MCP_VERIFICATION_KEYS
                or not isinstance(verification["status"], str)
                or not isinstance(verification["checked_at"], str)
                or not verification["checked_at"]):
            raise ValueError("MCP connector verification is invalid")

    return connectors


def _emit_mcp_connector_picker(connectors):
    """Emit the connector picker event when the Go TUI is available."""
    sys.stdout.write(json.dumps({
        "type": "tui_pause",
        "value": "mcp_connector_picker",
        "files": connectors,
    }) + "\n")
    sys.stdout.flush()


class InterruptedError(Exception):
    """Raised when the user interrupts execution mid-turn."""
    pass


_TOOL_TIMEOUT = float(os.getenv("KYREX_TOOL_TIMEOUT", "300"))


def _timeout_handler(func_name, result_holder, completed_event):
    if completed_event.is_set():
        return  # Tool already finished — don't overwrite result
    result_holder["error"] = f"Timeout executing tool '{func_name}' after {_TOOL_TIMEOUT}s"


def _run_tool_with_timeout(func, func_name, args, result_holder):
    try:
        # Validate required arguments are present before calling
        sig = inspect.signature(func)
        missing = []
        for name, param in sig.parameters.items():
            # Skip self, *args, and **kwargs
            if name == "self":
                continue
            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            # Check if it's a required parameter (no default) and not provided
            if param.default is inspect.Parameter.empty and name not in args:
                missing.append(name)
        if missing:
            result_holder["error"] = (
                f"Missing required arguments for '{func_name}': {', '.join(missing)}. "
                f"Received: {args}"
            )
            return
        result_holder["result"] = func(**args)
    except Exception as e:
        result_holder["error"] = str(e)



INTERRUPT_MSG = "[USER INTERRUPTED] Address the new input directly. Do not resume prior tool operations unless explicitly told to continue."


BEHAVIOR_RULES = """ABSOLUTE RULES - never violated:
- Never reference how many times a question has been asked
- Never express frustration, impatience, or suggest the user is repeating themselves
- Never truncate answers due to perceived repetition
- Stay focused on the current question. Respond contextually — do not re-explain settled topics.
- ALWAYS call read_local_file on the target file immediately before calling edit_file. Never rely on previously seen file content as search_text.
- For multi-step requests, state the task list in your first response, then check off each item as you complete it.
- You are Kyrex, a terminal AI agent. You are not OpenCode."""

# Workspace root follows current working directory so Kyrex adapts
# to wherever it's invoked from, not where the binary lives.
_WORKSPACE_ROOT = os.getcwd()


def build_workspace_file_tree(root, max_depth=5, max_files=200):
    """Build a compact indented file tree string for the workspace.
    
    Returns a string with directories shown as headers and files indented
    beneath them. Skips common ignore directories. Truncates at max_files
    with a summary message.
    """
    ignore = {
        ".git", ".px_sessions", "__pycache__", "venv", "build_venv", "node_modules",
        ".venv", "dist", "build", ".px", "kyrex-vscode", ".kyrex_sessions",
    }
    collected = []  # list of (depth, parts_tuple)
    _MAX_COLLECT = 500  # safety limit during collection (prevents OOM on huge repos)

    def walk(path, depth=0):
        if depth > max_depth or len(collected) >= _MAX_COLLECT:
            return
        try:
            for p in sorted(path.iterdir()):
                if p.name in ignore:
                    continue
                if p.is_file():
                    parts = p.relative_to(Path(root)).parts
                    collected.append((len(parts) - 1, parts))
                elif p.is_dir():
                    walk(p, depth + 1)
        except PermissionError:
            pass

    walk(Path(root))

    total = len(collected)
    if total > max_files:
        collected = collected[:max_files]

    # Sort by relative path for deterministic tree layout
    collected.sort(key=lambda x: x[1])

    # Render as indented tree
    lines = []
    rendered_dirs = set()

    for _depth, parts in collected:
        # Emit any new directory headers
        for i in range(len(parts) - 1):
            dir_path = parts[:i + 1]
            if dir_path not in rendered_dirs:
                rendered_dirs.add(dir_path)
                indent = "  " * i
                lines.append(f"{indent}{dir_path[-1]}/")

        # Emit the file
        indent = "  " * (len(parts) - 1)
        lines.append(f"{indent}{parts[-1]}")

    if total > max_files:
        lines.append(f"... ({total - max_files} more files)")

    return "\n".join(lines)


class PlaneExecute:
    def __init__(self, provider: str | None = None, api_key: str | None = None, base_url: str | None = None, model: str | None = None, config=None):
        self._config = config
        if not self._config:
            from .config import ConfigManager
            self._config = ConfigManager()
            self._config.load()

        if self._config:
            provider = provider or self._config.get_provider()
            api_key = api_key or self._config.get_api_key()
            base_url = base_url or self._config.get("base_url")
            model = model or self._config.get("model")

        self.model = model or os.getenv("KYREX_MODEL")
        self.provider: BaseProvider = get_provider(
            provider,
            api_key,
            base_url=base_url,
            extra_headers=config.get_headers() if config else {},
        )
        self.session = TreeSessionManager()
        self.tools = ToolBox(self)
        self.skills = SkillsLoader()
        self.mcp = MCPManager()
        
        surface = os.environ.get("KYREX_SURFACE", "terminal")
        if surface == "cloud":
            identity_line = (
                "You are Kyrex, running autonomously in a cloud environment with no human "
                "watching in real time. Your edits are auto-approved as you make them, and your "
                "work will be reviewed afterward via a pull request, not live. "
            )
            style_line = (
                "Your responses should be clear and self-contained, since no one is watching your "
                "work as it happens — write as a final report a reviewer will read afterward, not as "
                "a live back-and-forth conversation. "
            )
        else:
            identity_line = (
                f"You are Kyrex. A terminal AI coding agent embedded directly in the user's {surface}. "
                "You work alongside the user like a senior engineer sitting next to them — fast, direct, and context-aware. "
            )
            style_line = (
                "Your responses should be conversational, friendly, and natural — "
                "explain things as you would to a colleague sitting next to you, not as a dry documentation page. "
            )
        self._system_prompt = (
            identity_line +
            "Execute first, explain later. Use tools for all actions. "

            # ── Active file awareness ──
            "When a ACTIVE FILE CONTEXT system message is present in the conversation, "
            "you already have that file loaded — acknowledge it naturally and use it without being asked. "
            "Never tell the user to 'drop a file path' if an active file context is already present. "

            # ── File reading behavior ──
            "When asked about the current project or codebase, use read_local_file and list_local_files "
            "to read actual source files directly. Do not rely solely on query_knowledge or query_memory — "
            "if those return nothing, proceed to read the relevant files from the file tree. "

            # ── Response style ──
            + style_line +
            "Avoid excessive bullet points, tables, or rigid formatting unless the user explicitly asks for them. "
            "Be concise. Don't over-explain settled topics. "

            # ── Multi-step task tracking ──
            "When handling any multi-step task, state the task list in your first response as a numbered checklist. "
            "Check off each item (e.g., [x] or ✅) as you complete it. Keep the list to 3-6 tasks. "
            "For simple single-step questions, skip the task list. "
            "When you have fully completed the user's request, you MUST call the task_complete tool "
            "with a brief summary of what was accomplished. Do NOT return empty tool_calls and assume "
            "the task is done — explicitly call task_complete to signal completion."
        )
        self.context_limit = int(os.getenv("KYREX_CONTEXT_LIMIT", "128000"))
        self._recursion_depth = 0
        self._max_recursion = int(os.getenv("KYREX_MAX_RECURSION", "25"))
        self.show_thinking = True
        if config:
            val = config.get("show_thinking")
            if val is not None:
                self.show_thinking = val
        self._stream_handler = None
        self._reasoning_handler = None
        self._final_round_handler = None  # Progressive final-round detection
        self._on_tool_start = None
        self._on_tool_result = None
        self._confirm_handler = None
        self.audit_enabled = True
        if config:
            val = config.get("audit_enabled")
            if val is not None:
                val = val if isinstance(val, bool) else str(val).lower() in ("true", "1")
                self.audit_enabled = val
        self.audit = ReasoningAuditLogger(enabled=self.audit_enabled)
        self._loop_strike = 0
        self._load_initial_state()

        # ── Token usage tracking ──────────────────────────────
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0
        self._compaction_count = 0
        self._last_compaction_before = 0
        self._last_compaction_after = 0

        # ── Interrupt signal ──────────────────────────────────
        # threading.Event checked at every loop boundary and during tool execution.
        # Set by the bridge when the user presses Esc during a running turn.
        self._interrupt_event = threading.Event()
        # Remains True after the engine exits an interrupted turn, so the bridge
        # can tell the turn was cancelled even if the event was cleared.
        self._interrupted_this_turn = False

    def _load_initial_state(self):
        is_fresh = not self.session.load("main")
        self._bootstrap_context(is_fresh=is_fresh)
        self.skills.discover()
        self.mcp.start_all()

    def _bootstrap_context(self, is_fresh=False):
        if not is_fresh:
            self._deduplicate_file_trees()
            return
        try:
            file_tree = build_workspace_file_tree(_WORKSPACE_ROOT)
        except Exception:
            file_tree = "[unable to list files]"

        ctx = f"## Working Directory: {_WORKSPACE_ROOT}\n## Local File Tree:\n{file_tree}"
        first_content = self._system_prompt + "\n\n" + BEHAVIOR_RULES + "\n\n" + ctx

        # Auto-load permanent agent rules if the skill exists
        agent_rules = self.skills.get("agent_rules")
        if agent_rules:
            first_content += "\n\n" + agent_rules.instructions

        # In a fresh session, just add it.
        self.session.append({"role": "system", "content": first_content})

    def _deduplicate_file_trees(self):
        """Remove duplicate file tree + rules system messages, keeping only the most recent."""
        deduped = []
        file_tree_found = False
        # Iterate in reverse to keep the MOST RECENT file tree
        for msg in reversed(self.session.history):
            if msg.get("role") == "system":
                content = msg.get("content", "")
                if "## Local File Tree:" in content and "ABSOLUTE RULES" in content:
                    if file_tree_found:
                        continue
                    file_tree_found = True
            deduped.append(msg)

        new_history = list(reversed(deduped))
        if len(new_history) < len(self.session.history):
            self.session.history = new_history

    

    def _prior_turn_had_tools(self):
        for msg in reversed(self.session.history):
            if msg.get("role") == "assistant":
                return bool(msg.get("tool_calls"))
        return False

    def interrupt(self):
        """Signal the engine to stop the current turn immediately."""
        self._interrupt_event.set()
        self._interrupted_this_turn = True

    def _check_interrupt(self):
        """Raise InterruptedError if the user has signaled an interrupt."""
        if self._interrupt_event.is_set():
            raise InterruptedError("User interrupted")

    def _sanitize_history(self, history):
        """Remove orphaned tool_calls that have no matching tool responses."""
        sanitized = []
        i = 0
        while i < len(history):
            msg = history[i]
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                # Check if next messages contain tool responses for all tool_calls
                tool_ids = {tc.get("id") for tc in msg["tool_calls"] if tc.get("id")}
                j = i + 1
                found_ids = set()
                while j < len(history) and history[j].get("role") == "tool":
                    found_ids.add(history[j].get("tool_call_id"))
                    j += 1
                if tool_ids and not tool_ids.issubset(found_ids):
                    # Orphaned tool call — strip tool_calls from this message
                    clean = dict(msg)
                    clean.pop("tool_calls", None)
                    if not clean.get("content"):
                        clean["content"] = "..."
                    sanitized.append(clean)
                    i += 1
                    continue
            sanitized.append(msg)
            i += 1
        return sanitized

    def _build_api_messages(self):
        self._deduplicate_file_trees()
        history = self._sanitize_history(self.session.history)
        api_messages = []
        system_contents = []

        for i, msg in enumerate(history):
            role = msg.get("role")
            content = msg.get("content") or ""

            if role == "system":
                if content:
                    system_contents.append(content)
            elif role == "assistant":
                content = msg.get("content") or ""
                m = {"role": "assistant", "content": content}
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    m["tool_calls"] = tool_calls

                # Restore reasoning_content as required by reasoning models (DeepSeek/Kimi)
                # Kimi K2.6 requires this property on historical tool turns
                m["reasoning_content"] = msg.get("reasoning_content") or msg.get("reasoning") or ""

                # Moonshot/Kimi validation: assistant message 'content' must not be empty if no tool_calls
                if not m["content"] and not tool_calls:
                    m["content"] = "..."

                api_messages.append(m)
            elif role == "tool":
                # Ensure tool messages only follow assistant messages with tool_calls or other tool messages
                if api_messages and (api_messages[-1].get("role") == "assistant" and api_messages[-1].get("tool_calls")) or (api_messages and api_messages[-1].get("role") == "tool"):
                    api_messages.append(msg)
            elif role == "user":
                api_messages.append({"role": "user", "content": content})
            else:
                # Fallback for any other roles
                api_messages.append(msg)

        # Consolidate all system messages into one at the beginning
        if system_contents:
            consolidated = "\n\n---\n\n".join(system_contents)
            api_messages.insert(0, {"role": "system", "content": consolidated})
            # Debug logging removed

        return api_messages

    def _check_context_compaction(self):
        approx_tokens = sum(len(json.dumps(m)) for m in self.session.history) // 4
        if approx_tokens > self.context_limit * 0.8:
            compact = []
            # Keep the last 15 messages untouched to avoid breaking active tool sequences or recent context
            preserve_tail = 15
            history = self.session.history
            to_process = history[:-preserve_tail] if len(history) > preserve_tail else []
            tail = history[-preserve_tail:] if len(history) > preserve_tail else history

            for m in to_process:
                role = m.get("role", "")
                if role in ("system", "user"):
                    compact.append(m)
                elif role == "assistant":
                    content = m.get("content", "") or ""
                    tool_calls = m.get("tool_calls")
                    if tool_calls:
                        # Keep tool_calls but truncate content if present
                        names = [tc["function"]["name"] for tc in tool_calls]
                        new_msg = {"role": "assistant", "content": f"[called tools: {', '.join(names)}]"}
                        if content:
                            new_msg["content"] = (content[:100] + "...") if len(content) > 100 else content
                        new_msg["tool_calls"] = tool_calls
                        compact.append(new_msg)
                    elif len(content) > 200:
                        compact.append({"role": "assistant", "content": content[:200] + "..."})
                    else:
                        compact.append(m)
                elif role == "tool":
                    content = str(m.get("content", ""))
                    # Increase truncation limit for tool results to avoid losing search/read data
                    limit = 2000
                    if len(content) > limit:
                        new_m = dict(m)
                        new_m["content"] = content[:limit] + f"... [truncated {len(content)-limit} chars]"
                        compact.append(new_m)
                    else:
                        compact.append(m)

            compact.extend(tail)

            if len(compact) < len(self.session.history):
                self._last_compaction_before = len(self.session.history)
                self.session.history = compact
                self._last_compaction_after = len(compact)
                self._compaction_count += 1
                print("[*] Context compacted.")

    def _get_all_tools_schema(self):
        schemas = []
        for name, cfg in BUILTIN_TOOLS.items():
            schemas.append({"type": "function", "function": {"name": name, **cfg}})
        schemas.extend(ext_registry.to_openai_schemas())
        schemas.extend(self.mcp.get_tool_schemas())
        return schemas

    async def chat(self, user_input=None):
        try:
            # Clear interrupt state at the start of every turn
            self._interrupt_event.clear()
            self._interrupted_this_turn = False

            is_recursing = self._recursion_depth > 0
            if not is_recursing:
                if user_input and user_input.startswith("/"):
                    return self.handle_command(user_input)

                if self._prior_turn_had_tools() and user_input:
                    lower = (user_input or "").strip().lower()
                    first_word = lower.split()[0] if lower else ""
                    if first_word not in ("go", "continue", "proceed", "next", "ok", "okay", "yes", "y", "apply"):
                        self.session.append({"role": "system", "content": INTERRUPT_MSG})

                matched = self.skills.match(user_input or "")
                if matched:
                    self.session.append({"role": "system", "content": f"[SKILL: {matched.name}] {matched.instructions}"})

                if user_input:
                    self.session.append({"role": "user", "content": user_input})

            self._recursion_depth += 1
            if self._recursion_depth > self._max_recursion:
                self._recursion_depth = 0
                self.session.save()
                return "[!] Max recursion depth reached.", ""

            # Reset consecutive empty rounds counter at start of new turn
            self._consecutive_empty_rounds = 0

            collected_content = []
            collected_reasoning = []
            last_tool_call_fingerprint = None

            for _ in range(self._max_recursion):
                self._check_interrupt()
                self._check_context_compaction()
                tools = self._get_all_tools_schema()

                # Use default stream handler if none provided
                streamer = self._stream_handler or (lambda x: print(x, end="", flush=True))

                # Build messages and estimate prompt tokens (~4 chars per token)
                api_messages = self._build_api_messages()
                prompt_est = sum(len(json.dumps(m)) for m in api_messages) // 4

                # Async provider invocation with streaming support
                response_dict = await self.provider.chat(
                    model=self.model,
                    messages=api_messages,
                    tools=tools,
                    stream_callback=streamer,
                    reasoning_callback=self._reasoning_handler,
                    interrupt_event=self._interrupt_event,
                    final_round_callback=self._final_round_handler,
                )

                reasoning = response_dict.get("reasoning_content") or response_dict.get("reasoning")
                if reasoning:
                    collected_reasoning.append(reasoning)
                    self.audit.start_block(reasoning, os.getcwd())
                if reasoning and self.show_thinking and not self._stream_handler:
                    sys.stderr.write("\n--- THOUGHT ---\n")
                    sys.stderr.write(reasoning.strip() + "\n")
                    print("---------------\n")

                # Filter out task_complete from tool_calls before storing in session
                raw_tool_calls = response_dict.get("tool_calls") or []
                task_complete_called = False
                task_complete_summary = ""
                active_tool_calls = []

                for tc in raw_tool_calls:
                    func_name = tc.get("function", {}).get("name", "")
                    if func_name == "task_complete":
                        task_complete_called = True
                        try:
                            args = json.loads(tc.get("function", {}).get("arguments", "{}"))
                            task_complete_summary = args.get("summary", "Task completed")
                        except Exception:
                            task_complete_summary = "Task completed"
                    else:
                        active_tool_calls.append(tc)

                # Store filtered response (without task_complete) in session
                msg_dict = dict(response_dict)
                if active_tool_calls:
                    msg_dict["tool_calls"] = active_tool_calls
                elif "tool_calls" in msg_dict:
                    del msg_dict["tool_calls"]
                self.session.append(msg_dict)

                content = response_dict.get("content")
                if content:
                    collected_content.append(content)

                # Accumulate token estimates
                self._total_prompt_tokens += prompt_est
                completion_est = (len(content or "") + len(reasoning or "")) // 4
                self._total_completion_tokens += completion_est

                # If task_complete was called, break explicitly
                if task_complete_called:
                    collected_content.append(f"\n[Task Complete: {task_complete_summary}]")
                    break

                # Track consecutive rounds with no tool calls
                if not active_tool_calls:
                    if not hasattr(self, '_consecutive_empty_rounds'):
                        self._consecutive_empty_rounds = 0
                    self._consecutive_empty_rounds += 1

                    # Allow up to 2 consecutive empty rounds (model might be "thinking")
                    # After that, assume task is complete to prevent infinite loop
                    if self._consecutive_empty_rounds >= 2:
                        collected_content.append("\n[Task assumed complete after 2 empty rounds]")
                        break
                    # Otherwise, continue to next round (model might resume tool usage)
                else:
                    # Reset counter when model uses tools
                    self._consecutive_empty_rounds = 0

                tool_calls = active_tool_calls

                # Loop detection: fingerprint the tool calls to catch repeated identical actions
                fingerprint = json.dumps([{
                    "n": tc.get("function", {}).get("name"),
                    "a": tc.get("function", {}).get("arguments")
                } for tc in tool_calls], sort_keys=True)

                if fingerprint == last_tool_call_fingerprint:
                    self._loop_strike += 1
                else:
                    self._loop_strike = 0

                if self._loop_strike >= 3:
                    msg = "[!] Loop detected: repeating identical tool calls 3+ times. Aborting reasoning loop."
                    print(f"\n{msg}")
                    collected_content.append(f"\n{msg}")
                    self._loop_strike = 0
                    break
                last_tool_call_fingerprint = fingerprint

                if content and streamer and content.strip():
                    streamer("\n\n---\n")

                any_success = False
                consecutive_failures = 0
                for tc in tool_calls:
                    self._check_interrupt()
                    func_name = "unknown"
                    result = None
                    try:
                        func_data = tc.get("function", {})
                        func_name = func_data.get("name", "")
                        raw_args = func_data.get("arguments", "{}")
                        try:
                            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                        except json.JSONDecodeError as je:
                            args = {}
                            result = (
                                f"JSON parse error in arguments for '{func_name}': {je}. "
                                f"Raw: {str(raw_args)[:200]}"
                            )
                            self.session.append({
                                "role": "tool",
                                "tool_call_id": tc.get("id", "call_unknown"),
                                "name": func_name,
                                "content": result,
                            })
                            consecutive_failures += 1
                            if hasattr(self, '_on_tool_result') and self._on_tool_result:
                                self._on_tool_result(func_name, {"error": result})
                            if consecutive_failures >= 3:
                                collected_content.append("[!] Circuit breaker: 3 consecutive tool failures. Aborting.")
                                break
                            continue

                        self.audit.record_tool_call(func_name, args)

                        if hasattr(self, '_on_tool_start') and self._on_tool_start:
                            self._on_tool_start(func_name, args)

                        result_holder = {}
                        completed_event = threading.Event()
                        timer = Timer(_TOOL_TIMEOUT, _timeout_handler, args=[func_name, result_holder, completed_event])
                        timer.start()

                        if ext_registry.get_tool(func_name):
                            thread = threading.Thread(target=_run_tool_with_timeout, args=(ext_registry.execute, func_name, args, result_holder))
                            thread.start()
                            # Interrupt-aware wait: poll every 100ms instead of blocking
                            deadline = time.monotonic() + _TOOL_TIMEOUT
                            while thread.is_alive() and time.monotonic() < deadline:
                                if self._interrupt_event.is_set():
                                    break
                                thread.join(timeout=0.1)
                        elif func_name.startswith("mcp_"):
                            thread = threading.Thread(target=_run_tool_with_timeout, args=(self.mcp.call_tool, func_name, (func_name, args), result_holder))
                            thread.start()
                            deadline = time.monotonic() + _TOOL_TIMEOUT
                            while thread.is_alive() and time.monotonic() < deadline:
                                if self._interrupt_event.is_set():
                                    break
                                thread.join(timeout=0.1)
                        else:
                            thread = threading.Thread(target=_run_tool_with_timeout, args=(getattr(self.tools, func_name), func_name, args, result_holder))
                            thread.start()
                            deadline = time.monotonic() + _TOOL_TIMEOUT
                            while thread.is_alive() and time.monotonic() < deadline:
                                if self._interrupt_event.is_set():
                                    break
                                thread.join(timeout=0.1)

                        completed_event.set()  # Signal timeout handler that tool finished
                        timer.cancel()
                        timer.join(timeout=1)

                        if "error" in result_holder:
                            raise TimeoutError(result_holder["error"])
                        result = result_holder.get("result")
                        any_success = True
                        consecutive_failures = 0

                        if hasattr(self, '_on_tool_result') and self._on_tool_result:
                            self._on_tool_result(func_name, result)

                    except TimeoutError as e:
                        result = f"Error executing tool '{func_name}': {str(e)}"
                        consecutive_failures += 1
                        if hasattr(self, '_on_tool_result') and self._on_tool_result:
                            self._on_tool_result(func_name, {"error": str(e)})
                    except Exception as e:
                        result = f"Error executing tool '{func_name}': {str(e)}"
                        consecutive_failures += 1
                        if hasattr(self, '_on_tool_result') and self._on_tool_result:
                            self._on_tool_result(func_name, {"error": str(e)})
                    self.session.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", "call_unknown"),
                        "name": func_name,
                        "content": str(result),
                    })

                    # Emit any buffered diffs now that the tool result is returned
                    self.tools.flush_pending_diffs()

                    if consecutive_failures >= 3:
                        collected_content.append("[!] Circuit breaker: 3 consecutive tool failures. Aborting.")
                        break

                if not any_success:
                    break
            else:
                collected_content.append("\n[!] Max recursion depth reached.")

            self._recursion_depth = 0
            full_text = "\n".join(collected_content)
            full_reasoning = "\n\n---\n\n".join(collected_reasoning)

            if not full_text and collected_reasoning:
                full_text = "[Model produced reasoning but no display content. Check above output.]"

            if "```diff" in full_text or "```python" in full_text:
                pass  # Auto-apply — no confirmation prompt

            self.audit.flush(os.getcwd())
            self.session.save()
            return (full_text if full_text else ""), (full_reasoning if full_reasoning else "")

        except InterruptedError:
            # User pressed Esc — clean exit, save state, return empty
            self._recursion_depth = 0
            self._interrupt_event.clear()
            self.session.save()
            return "", ""

        except Exception as e:
            self._recursion_depth = 0
            err_msg = f"[!] Engine error: {str(e)}"
            print(err_msg)
            # Log full traceback to file instead of printing to stdout
            try:
                log_dir = Path.home() / ".kyrex"
                log_dir.mkdir(parents=True, exist_ok=True)
                log_path = log_dir / "error.log"
                import traceback as _tb
                import datetime
                with open(log_path, "a") as f:
                    f.write(f"\n--- {datetime.datetime.now()} ---\n")
                    _tb.print_exc(file=f)
                print(f"[*] Full error logged to {log_path}")
            except Exception:
                # Fallback: print traceback if logging fails
                import traceback as _tb
                _tb.print_exc()
            self.session.save()
            return err_msg, ""

    def get_usage_stats(self):
        """Return the current usage stats dict (same data as /usage)."""
        import json as _json
        history_count = len(self.session.history)
        current_est = sum(len(_json.dumps(m)) for m in self.session.history) // 4
        return {
            "prompt_tokens": self._total_prompt_tokens,
            "completion_tokens": self._total_completion_tokens,
            "history_messages": history_count,
            "compaction_events": self._compaction_count,
            "context_before": self._last_compaction_before,
            "context_after": self._last_compaction_after,
            "current_context_est": current_est,
            "context_limit": self.context_limit,
            "model": self.model,
            "provider": self.provider.name,
            "cost": _estimate_cost(self.model, self._total_prompt_tokens, self._total_completion_tokens),
        }

    def handle_command(self, cmd):
        parts = cmd.split()
        action = parts[0].lower()

        if action == "/branch":
            name = parts[1] if len(parts) > 1 else None
            self.session.branch(name)
            print(f"[*] Forked to new branch: {self.session.current_branch_name}")

        elif action in ("/new", "/clear"):
            self.session.save()
            system_prompt = (
                "You are Kyrex. A minimalist terminal agent. "
                "You focus on structural integrity and network reliability. "
                "Execute first, explain later. Use tools for all actions."
            )
            try:
                file_tree = build_workspace_file_tree(_WORKSPACE_ROOT)
            except Exception:
                file_tree = "[unable to list files]"
            ctx = f"## Working Directory: {_WORKSPACE_ROOT}\n## Local File Tree:\n{file_tree}"
            full_system = system_prompt + "\n\n" + BEHAVIOR_RULES + "\n\n" + ctx
            new_branch = self.session.reset_fresh(full_system, "", "")
            # Reset token tracking counters for the fresh session
            self._total_prompt_tokens = 0
            self._total_completion_tokens = 0
            self._compaction_count = 0
            self._last_compaction_before = 0
            self._last_compaction_after = 0
            # Overwrite "main" so next restart loads the clean session
            self.session.current_branch_name = "main"
            self.session.save("main")
            print(f"[*] Context cleared. Starting new session branch: {new_branch}")

        elif action == "/checkout":
            if len(parts) < 2:
                print("[!] Usage: /checkout <branch_name>")
                return "", ""
            if self.session.checkout(parts[1]):
                print(f"[*] Switched to branch: {parts[1]}")
            else:
                print(f"[!] Branch '{parts[1]}' not found.")

        elif action == "/tree":
            branches = self.session.list_branches()
            print("\nSession Tree:")
            for b in branches:
                prefix = "-> " if b == self.session.current_branch_name else "   "
                print(f"{prefix}{b}")

        elif action == "/undo":
            # Rewind to the last user message
            found_user = -1
            for i in range(len(self.session.history) - 1, -1, -1):
                if self.session.history[i].get("role") == "user":
                    found_user = i
                    break

            if found_user != -1:
                self.session.history = self.session.history[:found_user]
                self.session.recalculate_token_count()
                self.session.save()
                print(f"[*] Rewound history. Removed last interaction starting at index {found_user}.")
            else:
                print("[!] No user message found to undo.")

        elif action == "/export":
            html = self.session.export_html()
            path = Path("session_export.html")
            path.write_text(html)
            print(f"[*] Session exported to {path.resolve()}")

        elif action == "/bookmark":
            if len(parts) < 2:
                print("[!] Usage: /bookmark <label>")
                return "", ""
            self.session.bookmark(" ".join(parts[1:]))
            print(f"[*] Bookmarked: {' '.join(parts[1:])}")

        elif action == "/skill":
            if len(parts) < 2:
                skills = self.skills.discover()
                if skills:
                    print("Available skills:")
                    for name, sk in skills.items():
                        print(f"  {name}: {sk.description}")
                else:
                    print("[!] No skills found. Create .md files in ~/.kyrex/skills/ or .px_skills/")
                return "", ""
            skill = self.skills.get(parts[1])
            if skill:
                self.session.append({"role": "system", "content": f"[SKILL LOADED: {skill.name}] {skill.instructions}"})
                self.session.save()
                print(f"[*] Skill '{skill.name}' loaded: {skill.description}")
            else:
                print(f"[!] Skill '{parts[1]}' not found.")

        elif action == "/spawn":
            if len(parts) < 2:
                print("[!] Usage: /spawn <prompt>")
                return "", ""
            prompt = " ".join(parts[1:])
            import subprocess
            result = subprocess.run(
                [sys.argv[0] or "px", "-p", prompt],
                capture_output=True, text=True, timeout=60,
            )
            print(f"[*] Spawn result:\n{result.stdout.strip()}")
            if result.stderr:
                print(f"[!] Stderr:\n{result.stderr.strip()}")

        elif action in ("/mcp", "/mcp-browse"):
            if action == "/mcp-browse":
                try:
                    connectors = _load_mcp_connector_manifest()
                    if _is_interactive():
                        _emit_mcp_connector_picker(connectors)
                    else:
                        print("MCP connectors (use /mcp-browse in the Kyrex TUI to pick one):")
                        for connector in connectors:
                            auth_mode = connector["auth"]["mode"]
                            status = connector["verification"]["status"]
                            print(f"  {connector['id']}: {connector['name']} — {connector['description']} [{connector['command']}; auth={auth_mode}; {status}]")
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    print(f"[!] Could not load MCP connector catalog: {exc}")
            elif len(parts) < 2:
                print("MCP servers:")
                for name in self.mcp.servers:
                    print(f"  {name}")
                return "", ""
            elif parts[1] == "add" and len(parts) >= 4:
                self.mcp.add(parts[2], parts[3], parts[4:] if len(parts) > 4 else None)
                print(f"[*] MCP server '{parts[2]}' added.")
            elif parts[1] == "remove" and len(parts) >= 3:
                self.mcp.remove(parts[2])
                print(f"[*] MCP server '{parts[2]}' removed.")
            else:
                print("Usage: /mcp add <name> <command> [args...]")
                print("       /mcp remove <name>")

        elif action == "/model":
            if len(parts) < 2:
                # Emit tui_pause so the Go TUI can show an interactive model picker
                try:
                    import urllib.request
                    base_url = self._config.get("base_url") or ""
                    api_key = (self._config.get_api_key() or "").strip()
                    models_url = base_url.rstrip("/") + "/models"
                    req = urllib.request.Request(models_url, headers={"Authorization": f"Bearer {api_key}", "User-Agent": "kyrex/1.0"})
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        import json as _json
                        data = _json.loads(resp.read())
                        models = [m["id"] for m in (data if isinstance(data, list) else data.get("data", []))]
                    sys.stdout.write(json.dumps({
                        "type": "tui_pause",
                        "value": "model_picker",
                        "files": models,
                        "model": self.model,
                    }) + "\n")
                    sys.stdout.flush()
                except Exception as e:
                    print(f"Current model: {self.model}")
                    print(f"(Could not fetch model list: {e})")
                return "", ""
            # Allow selection by number or name
            selection = parts[1]
            try:
                import urllib.request, json as _json
                base_url = self._config.get("base_url") or ""
                api_key = self._config.get_api_key() or ""
                models_url = base_url.rstrip("/") + "/models"
                req = urllib.request.Request(models_url, headers={"Authorization": f"Bearer {api_key}"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = _json.loads(resp.read())
                    models = [m["id"] for m in (data if isinstance(data, list) else data.get("data", []))]
                if selection.isdigit():
                    idx = int(selection) - 1
                    if 0 <= idx < len(models):
                        new_model = models[idx]
                    else:
                        print(f"Invalid number. Pick 1-{len(models)}")
                        return "", ""
                else:
                    if selection not in models:
                        print(f"Warning: '{selection}' not in available models list. Switching anyway.")
                    new_model = selection
            except Exception:
                new_model = selection
            self.model = new_model
            if hasattr(self, '_config') and self._config:
                try:
                    self._config.save({"model": new_model})
                except Exception:
                    pass
            print(f"[*] Model switched to: {new_model}")
            # Emit session_state so TUI status bar updates immediately
            import json as _json
            import sys as _sys
            _sys.stdout.write(_json.dumps({
                "type": "session_state",
                "model": new_model,
                "provider": self._config.get_provider() or "openai",
                "context": str(__import__('os').getcwd()),
            }) + "\n")
            _sys.stdout.flush()

        elif action == "/usage":
            stats = self.get_usage_stats()
            sys.stdout.write(json.dumps({
                "type": "tui_pause",
                "value": "usage_stats",
                "files": stats,
            }) + "\n")
            sys.stdout.flush()
            return "", ""

        elif action == "/help":
            print("""KYREX COMMANDS:
SESSION: /branch [name]  /checkout <name>  /new  /clear  /tree  /undo  /bookmark <label>  /export
SKILLS:  /skill [name]
SPAWN:   /spawn <prompt>
MCP:     /mcp [add|remove] <name> [command] [args...]
MODEL:   /model [name]  Switch LLM model
HELP:    /help""")

        else:
            print(f"[!] Unknown command: {action}. Type /help for available commands.")

        return "", ""


# Approximate per-model pricing (USD per 1M tokens). Used for a best-effort
# cost estimate in the sidebar and /usage overlay. Prices are rough defaults
# and should be updated as providers change their rates.
_MODEL_PRICING = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (5.00, 15.00),
    "gpt-4-turbo": (10.00, 30.00),
    "gpt-4": (30.00, 60.00),
    "gpt-3.5-turbo": (0.50, 1.50),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-3-opus": (15.00, 75.00),
    "claude-3-sonnet": (3.00, 15.00),
    "claude-3-haiku": (0.25, 1.25),
    "o1-preview": (15.00, 60.00),
    "o1-mini": (3.00, 12.00),
}


def _estimate_cost(model, prompt_tokens, completion_tokens):
    """Return a formatted cost estimate string or '—' if the model is unknown."""
    if not model:
        return "—"
    name = model.lower().split("/")[-1]
    rates = None
    # Look for the most specific matching key first.
    for key in sorted(_MODEL_PRICING, key=lambda k: -len(k)):
        if key in name:
            rates = _MODEL_PRICING[key]
            break
    if rates is None:
        return "—"
    prompt_rate, completion_rate = rates
    cost = (prompt_tokens * prompt_rate + completion_tokens * completion_rate) / 1_000_000
    if cost < 0.01:
        return f"≈${cost:.4f}"
    return f"≈${cost:.2f}"
