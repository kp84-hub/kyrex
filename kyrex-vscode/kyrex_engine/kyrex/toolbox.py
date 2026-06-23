"""Tool implementations for Kyrex engine."""
import os
import sys
import json
import time
import uuid
import difflib
import subprocess
import re
import threading
from pathlib import Path
from typing import Optional

# ── VS Code edit proposal shared state ──
# Accessed by both toolbox (proposer) and core_bridge stdin_thread (resolver).
# The stdin_thread intercepts edit_decision messages directly, preventing
# deadlock where the async chat loop is blocked on Event.wait() and can't
# read stdin to resolve the edit.
_pending_edits: dict[str, threading.Event] = {}
_edit_results: dict[str, bool] = {}


def is_safe_path(target_path: str) -> bool:
    """Resolve target_path and ensure it strictly resides within os.getcwd()."""
    try:
        resolved = Path(target_path).resolve()
        cwd = Path(os.getcwd()).resolve()
        return resolved == cwd or cwd in resolved.parents
    except Exception:
        return False


def _is_interactive():
    """Check if running in an interactive frontend (TUI, VS Code, or raw terminal).

    The Python engine is spawned as a subprocess with piped stdin/stdout,
    so isatty() is always False even when the user is actively interacting
    through the Go TUI or VS Code. The frontend signals its presence via
    environment variables:
      - KYREX_SURFACE=terminal  (set by Go TUI bridge)
      - KYREX_VSCODE=1          (set by VS Code extension)
    """
    if os.environ.get("KYREX_SURFACE") == "terminal":
        return True
    if os.environ.get("KYREX_VSCODE") == "1":
        return True
    return sys.stdin.isatty() and sys.stdout.isatty()


class ToolBox:
    """Collection of tools available to the LLM."""
    
    def __init__(self, engine):
        self.engine = engine
        self._diff_counter = 0
        self._pending_diffs = []

    def _emit_diff_stream(self, path, diff_text):
        """Emit a diff stream message."""
        self._diff_counter += 1
        diff_id = f"diff_{self._diff_counter}_{int(time.time() * 1000)}"
        try:
            payload = json.dumps({
                "type": "diff",
                "id": diff_id,
                "path": str(path),
                "diff": diff_text,
            })
            sys.stdout.write(payload + "\n")
            sys.stdout.flush()
        except Exception:
            pass

    def _generate_diff(self, path, new_content):
        """Generate unified diff between current file and new content."""
        p = Path(path)
        if p.exists():
            old_lines = p.read_text().splitlines()
            new_lines = new_content.splitlines()
            diff = difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{p.name}", tofile=f"b/{p.name}", lineterm="")
            return "\n".join(diff)
        else:
            new_lines = new_content.splitlines()
            diff = difflib.unified_diff([], new_lines, fromfile="/dev/null", tofile=f"b/{p.name}", lineterm="")
            return "\n".join(diff)

    def _diff_gate(self, path, new_content):
        """Process diff and buffer it for emission."""
        p = Path(path)

        if p.exists():
            old = p.read_text().splitlines()
            new = new_content.splitlines()
            diff_lines = list(difflib.unified_diff(old, new, fromfile=f"a/{p.name}", tofile=f"b/{p.name}", lineterm="", n=3))
        else:
            new = new_content.splitlines()
            diff_lines = list(difflib.unified_diff([], new, fromfile="/dev/null", tofile=f"b/{p.name}", lineterm="", n=3))

        if not diff_lines:
            return True

        raw_diff = "\n".join(diff_lines)
        payload = json.dumps({"type": "diff", "id": "stream", "path": str(path), "diff": raw_diff})
        self._pending_diffs.append(payload)

        return True

    def flush_pending_diffs(self):
        """Emit all buffered diff output."""
        for payload in self._pending_diffs:
            sys.stdout.write(payload + "\n")
        if self._pending_diffs:
            sys.stdout.flush()
            self._pending_diffs.clear()

    def _propose_edit(self, path: str, content: str) -> bool:
        """
        Propose an edit to VS Code and block until the user decides.
        
        Emits a propose_edit JSON message to stdout with a unique edit_id,
        then blocks on threading.Event.wait() until the stdin_thread intercepts
        the corresponding edit_decision response.
        
        Returns True if accepted, False if rejected or timed out.
        """
        edit_id = str(uuid.uuid4())
        event = threading.Event()
        _pending_edits[edit_id] = event
        
        resolved_path = str(Path(path).resolve())
        payload = json.dumps({
            "type": "propose_edit",
            "editId": edit_id,
            "filePath": resolved_path,
            "content": content,
        })
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()
        
        # Block until stdin_thread resolves this edit (5 minute timeout)
        resolved = event.wait(timeout=300)
        
        # Clean up shared state
        accepted = _edit_results.pop(edit_id, False) if resolved else False
        _pending_edits.pop(edit_id, None)
        
        return accepted

    def write_file_with_gate(self, path, content):
        """Write file with AST validation for Python files."""
        import ast
        if path.endswith('.py'):
            try:
                ast.parse(content)
            except SyntaxError as e:
                return {"error": f"AST gate failed: {e}"}
        
        if os.environ.get("KYREX_VSCODE"):
            accepted = self._propose_edit(path, content)
            if accepted:
                Path(path).write_text(content)
                return {"status": "ok", "path": str(Path(path).resolve())}
            else:
                return {"error": "Edit rejected by user."}
        
        if not self._diff_gate(path, content):
            return {"error": "Update cancelled by user."}
        
        Path(path).write_text(content)
        return {"status": "ok", "path": str(path)}

    def edit_file(self, path, search_text, replace_text):
        """Edit file by replacing search_text with replace_text."""
        if not is_safe_path(path):
            return {"error": "SECURITY BLOCK: Access denied."}
        
        import ast
        p = Path(path)
        if not p.exists():
            return {"error": f"File not found: {path}"}
        
        content = p.read_text()
        count = content.count(search_text)
        
        if count == 0:
            norm_content = re.sub(r'\s+', ' ', content).strip()
            norm_search = re.sub(r'\s+', ' ', search_text).strip()
            if norm_search not in norm_content:
                return {"error": "search_text not found. Provide a larger unique context block."}
            if norm_content.count(norm_search) > 1:
                return {"error": f"search_text appears {norm_content.count(norm_search)} times. Needs more unique context."}
            parts = re.split(r'\s+', search_text.strip())
            pattern = r'\s+'.join(re.escape(part) for part in parts)
            new_content = re.sub(pattern, replace_text, content, count=1)
        elif count > 1:
            return {"error": f"search_text appears {count} times. Needs more unique context."}
        else:
            new_content = content.replace(search_text, replace_text, 1)
        
        if path.endswith('.py'):
            try:
                ast.parse(new_content)
            except SyntaxError as e:
                return {"error": f"AST gate failed: {e}"}
        
        if os.environ.get("KYREX_VSCODE"):
            accepted = self._propose_edit(path, new_content)
            if accepted:
                p.write_text(new_content)
                return {"status": "ok", "path": str(p.resolve())}
            else:
                return {"error": "Edit rejected by user."}
        
        if not self._diff_gate(path, new_content):
            return {"error": "Update cancelled by user."}
        
        p.write_text(new_content)
        return {"status": "ok", "path": str(p)}

    def search(self, pattern, path=".", extension=None):
        """Search for regex pattern in files."""
        hidden = {".git", ".px_sessions", ".vael_sessions", "venv", "__pycache__"}
        matches = []
        base = Path(path).resolve()

        if base.is_file():
            targets = [base]
        elif base.is_dir():
            targets = [p for p in base.rglob("*") if p.is_file()]
        else:
            targets = []

        for p in targets:
            if any(part in hidden for part in p.parts):
                continue
            if extension and p.suffix != extension:
                continue
            try:
                text = p.read_text(errors="ignore")
                for i, line in enumerate(text.splitlines(), 1):
                    if re.search(pattern, line):
                        matches.append(f"{p}:{i}:{line.strip()}")
                        if len(matches) >= 50:
                            return {"status": "ok", "results": matches}
            except Exception:
                continue
        
        return {"status": "ok", "results": matches}

    def query_memory(self, query):
        """Query memory for established patterns."""
        dirs = [Path(".px_memory"), Path(".px_docs")]
        keywords = ["lessons learned", "best practices", "architectural decisions"]
        best, best_score = None, 0
        
        for d in dirs:
            if not d.exists():
                continue
            for p in d.rglob("*.md"):
                try:
                    text = p.read_text(errors="ignore").lower()
                    score = sum(text.count(kw) for kw in keywords)
                    if score > best_score:
                        best_score = score
                        best = p
                except Exception:
                    continue
        
        primary = Path("px_knowledge.md")
        if primary.exists():
            best = primary
        
        if best:
            return {"status": "ok", "source": str(best), "content": best.read_text(errors="ignore")[:1000]}
        return {"status": "ok", "content": "No memory found. Proceeding with internal training.", "deviation": True}

    def query_knowledge(self, query):
        """Query .px_docs for project standards."""
        d = Path(".px_docs")
        if not d.exists():
            return {"status": "ok", "content": "No .px_docs directory found. Proceeding without local knowledge."}
        
        keywords = ["rules of engagement", "code standards", "architecture", "lessons learned"]
        best, best_score = None, 0
        
        for p in d.rglob("*.md"):
            score = sum(3 for kw in keywords if kw in p.name.lower())
            try:
                for line in p.read_text(errors="ignore").splitlines()[:20]:
                    score += sum(1 for kw in keywords if kw in line.lower())
            except Exception:
                continue
            if score > best_score:
                best_score = score
                best = p
        
        if best:
            return {"status": "ok", "source": str(best), "content": best.read_text(errors="ignore")[:1200]}
        return {"status": "ok", "content": "No local knowledge found. Proceeding with internal training."}

    def read_local_file(self, path, limit: Optional[int] = None, offset: Optional[int] = None):
        """Read file content.
        
        Args:
            path: File path to read
            limit: Maximum number of lines to return (from start or from offset)
            offset: Number of lines to skip from the beginning
        """
        if not is_safe_path(path):
            return {"error": "SECURITY BLOCK: Access denied."}
        
        p = Path(path)
        if not p.exists() or not p.is_file():
            return {"error": f"File not found: {path}"}
        
        content = p.read_text(errors="ignore")
        lines = content.splitlines()
        
        # Apply offset first (skip N lines)
        if offset is not None:
            # Clamp negative offsets to 0
            if offset < 0:
                offset = 0
            lines = lines[offset:]
        
        # Then apply limit (take N lines from what's left)
        if limit is not None:
            lines = lines[:limit]
        
        content = "\n".join(lines)
        return {"status": "ok", "path": str(p), "content": content}

    def list_local_files(self, directory="."):
        """List files in directory."""
        d = Path(directory)
        if not d.exists() or not d.is_dir():
            return {"error": f"Directory not found: {directory}"}
        
        hidden = {".git", ".px_sessions", ".vael_sessions", "venv", "__pycache__"}
        files = []
        for p in d.rglob("*"):
            if p.is_file():
                if not any(part in hidden for part in p.parts):
                    files.append(str(p))
                    if len(files) >= 1000:
                        break
        
        return {"status": "ok", "directory": str(d.resolve()), "files": files}

    def run_command(self, command):
        """Execute shell command."""
        cmd_lower = command.lower().strip()

        # ── Permanently blocked ──
        # Inline Python execution (python3 -c) is the primary bypass vector for
        # the deletion gate. File ops must go through edit_file/write_file_with_gate.
        blocked_patterns = [
            r'\brm\s+-\w*[rf]',
            r'\bdd\s+',
            r'\bmkfs\b',
            r'\bshutdown\b',
            r'\breboot\b',
            r'\bcurl\s+.*\|\s*(ba)?sh',
            r'\bwget\s+.*\|\s*(ba)?sh',
            r'\bpython[3]?\s+-c\b',
            r'\bpython[3]?\s*<<\b',
            r'\|\s*python[3]?\b',
        ]
        for pat in blocked_patterns:
            if re.search(pat, cmd_lower):
                return {"error": f"Command blocked for safety: '{command}'. This command is permanently forbidden."}

        needs_confirm = False
        confirm_reason = []

        if re.search(r'\bsudo\b', cmd_lower):
            needs_confirm = True
            confirm_reason.append("uses sudo")

        if re.search(r'\|\s*(ba)?sh\b', cmd_lower):
            needs_confirm = True
            confirm_reason.append("pipes to shell")

        if re.search(r'\brm\s+', cmd_lower) and not re.search(r'\brm\s+-\w*[rf]', cmd_lower):
            needs_confirm = True
            confirm_reason.append("deletes files")

        if re.search(r'\brmdir\b', cmd_lower):
            needs_confirm = True
            confirm_reason.append("deletes directories")

        if re.search(r'\bunlink\b', cmd_lower):
            needs_confirm = True
            confirm_reason.append("deletes files")

        if re.search(r'\bfind\b.*\b-delete\b', cmd_lower):
            needs_confirm = True
            confirm_reason.append("deletes files")

        if needs_confirm:
            reason_str = ", ".join(confirm_reason)
            if _is_interactive():
                sys.stderr.write(f"[!] Destructive command detected ({reason_str}): {command}\n")
                sys.stderr.write("    Proceed? [y/N] ")
                sys.stderr.flush()
                try:
                    answer = input().strip().lower()
                except (EOFError, KeyboardInterrupt):
                    answer = "n"
                if answer not in ("y", "yes"):
                    return {"error": f"Command cancelled by user: {command}"}
            else:
                return {
                    "error": f"Destructive command blocked in non-interactive mode ({reason_str}): {command}. "
                             f"Run interactively to confirm."
                }

        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=10,
                cwd=str(Path.cwd().resolve()),
            )
            output = result.stdout
            if result.stderr:
                output += "\n[stderr]\n" + result.stderr
            if len(output) > 8000:
                output = output[:8000] + f"\n... [truncated {len(output)-8000} chars]"
            return {
                "status": "ok",
                "command": command,
                "returncode": result.returncode,
                "output": output,
            }
        except subprocess.TimeoutExpired:
            return {"error": f"Command timed out after 10 seconds: {command}"}
        except Exception as e:
            return {"error": f"Failed to execute command: {str(e)}"}


# Built-in tool schemas
BUILTIN_TOOLS = {
    "edit_file": {
        "description": "Make a surgical edit to an existing file. Use write_file (not this) for creating new files. Returns AST-gated result.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to edit"},
                "search_text": {"type": "string", "description": "Unique text block to locate and replace"},
                "replace_text": {"type": "string", "description": "The replacement text"},
            },
            "required": ["path", "search_text", "replace_text"],
        },
    },
    "write_file_with_gate": {
        "description": "Create or overwrite a file with AST validation and human diff confirmation gate.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file"},
                "content": {"type": "string", "description": "The file content to write"},
            },
            "required": ["path", "content"],
        },
    },
    "search": {
        "description": "Recursively search for a regex pattern across files. Returns up to 50 matches.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "path": {"type": "string", "description": "Starting directory (default: .)"},
                "extension": {"type": "string", "description": "File extension filter (e.g. '.py')"},
            },
            "required": ["pattern"],
        },
    },
    "query_memory": {
        "description": "Query Kyrex's memory for established patterns and conventions.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "The question or topic to search"}},
            "required": ["query"],
        },
    },
    "query_knowledge": {
        "description": "Query .px_docs for project standards, architecture, and lessons learned.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Topic to search in .px_docs"}},
            "required": ["query"],
        },
    },
    "read_local_file": {
        "description": "Read the full content of a local file. Supports line offsets and limits.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file"},
                "limit": {"type": "integer", "description": "Optional: max number of lines to read"},
                "offset": {"type": "integer", "description": "Optional: number of lines to skip from beginning (0-indexed)"},
            },
            "required": ["path"],
        },
    },
    "list_local_files": {
        "description": "Recursively list all files in a local directory.",
        "parameters": {
            "type": "object",
            "properties": {"directory": {"type": "string", "description": "Directory to list"}},
            "required": [],
        },
    },
    "run_command": {
        "description": "Execute a shell command in the working directory. Captures stdout and stderr with a 10-second timeout. Dangerous commands (rm -rf, dd, mkfs, shutdown, reboot, curl|bash, wget|bash) are blocked. Destructive commands (sudo, pipes to sh, file deletion) require y/n confirmation.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "Shell command to execute"}},
            "required": ["command"],
        },
    },
}
