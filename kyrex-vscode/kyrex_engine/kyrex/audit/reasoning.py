import os
import json
import threading
from datetime import datetime


_FILE_OPS_READ = {"read_local_file", "list_local_files", "search", "query_memory", "query_knowledge"}
_FILE_OPS_WRITE = {"write_file_with_gate", "edit_file"}


class ReasoningAuditLogger:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self._lock = threading.Lock()
        self._current_block = None
        self._block_count = 0

    def start_block(self, reasoning_content: str, cwd: str) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            self._flush_locked(cwd)
            self._block_count += 1
            self._current_block = {
                "cwd": cwd,
                "timestamp": datetime.now(),
                "reasoning": reasoning_content,
                "files_read": [],
                "files_written": [],
                "tool_calls": [],
                "block_num": self._block_count,
            }
            return True

    def append_reasoning(self, text: str, cwd: str = None):
        if not self.enabled:
            return
        with self._lock:
            if self._current_block:
                self._current_block["reasoning"] += "\n\n" + text
            else:
                self.start_block(text, cwd or os.getcwd())

    def record_file_read(self, path: str):
        if not self.enabled:
            return
        with self._lock:
            if self._current_block and path not in self._current_block["files_read"]:
                self._current_block["files_read"].append(path)

    def record_file_write(self, path: str):
        if not self.enabled:
            return
        with self._lock:
            if self._current_block and path not in self._current_block["files_written"]:
                self._current_block["files_written"].append(path)

    def record_tool_call(self, tool_name: str, args: dict = None):
        if not self.enabled:
            return
        args = args or {}
        with self._lock:
            if self._current_block:
                self._current_block["tool_calls"].append({"name": tool_name, "args": args})

        path = str(args.get("path") or args.get("directory") or args.get("extension") or "")
        if tool_name in _FILE_OPS_READ:
            self.record_file_read(path or os.getcwd())
        elif tool_name in _FILE_OPS_WRITE:
            self.record_file_write(path)

    def flush(self, cwd: str = "."):
        if not self.enabled:
            return
        with self._lock:
            self._flush_locked(cwd)

    def _flush_locked(self, cwd: str):
        block = self._current_block
        self._current_block = None
        if not block or not block["reasoning"]:
            return
        try:
            ts = block["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
            tools_summary = []
            for tc in block["tool_calls"]:
                tools_summary.append(f"- {tc['name']}({json.dumps(tc['args'])})")
            
            lines = [
                f"## {ts} - {block['cwd']}",
                "",
                f"### Reasoning Block #{block['block_num']}",
                block["reasoning"].strip(),
                "",
                "### Tools Executed",
                "\n".join(tools_summary) if tools_summary else "(none)",
                "",
                "### Files Accessed",
                f"- Read: {', '.join(block['files_read']) if block['files_read'] else '(none)'}",
                f"- Written: {', '.join(block['files_written']) if block['files_written'] else '(none)'}",
                "",
                "---",
                "",
            ]
            history_path = os.path.join(cwd, ".px_history")
            with open(history_path, "a") as f:
                f.write("\n".join(lines))
        except Exception:
            pass
