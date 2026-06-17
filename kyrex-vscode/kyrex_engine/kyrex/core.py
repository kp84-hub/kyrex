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
from .toolbox import ToolBox, BUILTIN_TOOLS


class InterruptedError(Exception):
    """Raised when the user interrupts execution mid-turn."""
    pass


_TOOL_TIMEOUT = float((os.getenv("KYREX_TOOL_TIMEOUT") or os.getenv("VAEL_TOOL_TIMEOUT") or "300"))


def _timeout_handler(func_name, result_holder):
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



MODE_RULES = {
    "plan": "PLAN MODE: Use tools only when the user explicitly asks you to check the code. Prioritize concise, direct answers.",
    "execute": "EXECUTE MODE: Work efficiently and execute tasks. Use tools proactively to complete the work.",
}

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
        self.mode = (config.get("DEFAULT_MODE") if config else None) or os.getenv("KYREX_MODE", "plan")
        self._system_prompt = (
             "You are Kyrex. A terminal AI coding agent embedded directly in the user's VS Code editor. "
            "You work alongside the user like a senior engineer sitting next to them — fast, direct, and context-aware. "
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
            "Your responses should be conversational, friendly, and natural — "
            "explain things as you would to a colleague sitting next to you, not as a dry documentation page. "
            "Avoid excessive bullet points, tables, or rigid formatting unless the user explicitly asks for them. "
            "Be concise. Don't over-explain settled topics. "

            # ── Multi-step task tracking ──
            "When handling any multi-step task, state the task list in your first response as a numbered checklist. "
            "Check off each item (e.g., [x] or ✅) as you complete it. Keep the list to 3-6 tasks. "
            "For simple single-step questions, skip the task list."
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
            ignore = {".git", ".px_sessions", "__pycache__", "venv", "node_modules", ".venv"}
            tree_lines = []
            def walk(path, depth=0):
                if depth > 5 or len(tree_lines) > 500:
                    return
                for p in path.iterdir():
                    if p.name in ignore:
                        continue
                    if p.is_file():
                        tree_lines.append(str(p))
                    elif p.is_dir():
                        walk(p, depth + 1)

            walk(Path(_WORKSPACE_ROOT))

            if len(tree_lines) > 200:
                total = len(tree_lines)
                tree_lines = tree_lines[:200] + [f"... ({total - 200} more files)"]
            file_tree = "\n".join(tree_lines)
        except Exception:
            file_tree = "[unable to list files]"

        ctx = f"## Working Directory: {_WORKSPACE_ROOT}\n## Local File Tree:\n{file_tree}"
        first_content = self._system_prompt + "\n\n" + BEHAVIOR_RULES + "\n\n" + MODE_RULES[self.mode] + "\n\n" + ctx

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

    def _mode_prompt(self) -> str:
        return MODE_RULES.get(self.mode, MODE_RULES["plan"])

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
                )

                reasoning = response_dict.get("reasoning_content") or response_dict.get("reasoning")
                if reasoning:
                    collected_reasoning.append(reasoning)
                    self.audit.start_block(reasoning, os.getcwd())
                if reasoning and self.show_thinking and not self._stream_handler:
                    sys.stderr.write("\n--- THOUGHT ---\n")
                    sys.stderr.write(reasoning.strip() + "\n")
                    print("---------------\n")

                msg_dict = dict(response_dict)
                self.session.append(msg_dict)

                content = response_dict.get("content")
                if content:
                    collected_content.append(content)

                # Accumulate token estimates
                self._total_prompt_tokens += prompt_est
                completion_est = (len(content or "") + len(reasoning or "")) // 4
                self._total_completion_tokens += completion_est

                tool_calls = response_dict.get("tool_calls")
                if not tool_calls:
                    break

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
                        timer = Timer(_TOOL_TIMEOUT, _timeout_handler, args=[func_name, result_holder])
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
            err_msg = f"[!] EXCEPTION CAUGHT: {str(e)}"
            print(err_msg)
            import traceback
            traceback.print_exc()
            self.session.save()
            return err_msg, ""

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
                ignore = {".git", ".px_sessions", "__pycache__", "venv", "node_modules", ".venv",
                          "dist", "build", ".px", "kyrex-vscode", ".kyrex_sessions"}
                files = []
                for p in Path(_WORKSPACE_ROOT).rglob("*"):
                    if p.is_file() and not any(part in ignore for part in p.parts):
                        files.append(str(p))
                tree_lines = files[:200]
                if len(files) > 200:
                    tree_lines += [f"... ({len(files) - 200} more files)"]
                file_tree = "\n".join(tree_lines)
            except Exception:
                file_tree = "[unable to list files]"
            ctx = f"## Working Directory: {_WORKSPACE_ROOT}\n## Local File Tree:\n{file_tree}"
            full_system = system_prompt + "\n\n" + BEHAVIOR_RULES + "\n\n" + MODE_RULES[self.mode] + "\n\n" + ctx
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
                    print("[!] No skills found. Create .md files in ~/.vael/skills/ or .px_skills/")
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

        elif action == "/mcp":
            if len(parts) < 2:
                print("MCP servers:")
                for name in self.mcp.servers:
                    print(f"  {name}")
                return "", ""
            if parts[1] == "add" and len(parts) >= 4:
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
            import json as _json
            history_count = len(self.session.history)
            current_est = sum(len(_json.dumps(m)) for m in self.session.history) // 4
            stats = {
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
            }
            sys.stdout.write(_json.dumps({
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
MODE:    /mode       Toggle plan/execute
MODEL:   /model [name]  Switch LLM model
HELP:    /help""")

        elif action == "/mode":
            new = self.toggle_mode()
            print(f"[*] Switched to {new.upper()} mode")

        else:
            print(f"[!] Unknown command: {action}. Type /help for available commands.")

        return "", ""

    def toggle_mode(self) -> str:
        self.mode = "execute" if self.mode == "plan" else "plan"
        for i, msg in enumerate(self.session.history):
            content = msg.get("content") or ""
            if msg.get("role") == "system" and ("PLAN MODE:" in content or "EXECUTE MODE:" in content):
                new_content = content.replace("PLAN MODE:", "$$$PLACEHOLDER$$$").replace("EXECUTE MODE:", "PLAN MODE:").replace("$$$PLACEHOLDER$$$", "EXECUTE MODE:")
                self.session.history[i] = {"role": "system", "content": new_content}
                break
        self.session.save()
        return self.mode
