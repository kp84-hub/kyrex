import json
import time
from pathlib import Path


class TreeSessionManager:
    def __init__(self, base_path: str = ".px_sessions"):
        self.base_path = Path(base_path)
        self.base_path.mkdir(exist_ok=True)
        self.history: list = []
        self.approx_tokens: int = 0
        self._branch_fork: dict[str, int] = {"main": 0}
        self.current_branch_name: str = "main"
        self._labels: dict[int, str] = {}

    def append(self, message: dict):
        self.history.append(message)
        self.approx_tokens += len(json.dumps(message)) // 4

    def get_history(self) -> list:
        return list(self.history)

    def fork_point(self) -> int:
        return len(self.history)

    def bookmark(self, label: str):
        if not self.history:
            return  # Can't bookmark an empty history
        self._labels[len(self.history) - 1] = label

    def get_bookmarks(self) -> dict[int, str]:
        return dict(self._labels)

    def branch(self, branch_name: str | None = None) -> str:
        name = branch_name or f"branch_{int(time.time())}"
        self.save()
        self._branch_fork[name] = len(self.history)
        self.current_branch_name = name
        self.save()
        return name

    def checkout(self, branch_name: str) -> bool:
        if branch_name not in self._branch_fork:
            return False
        self.save()
        if not self.load(branch_name):
            self.current_branch_name = branch_name
        return True

    def recalculate_token_count(self):
        """Recalculate the approximate token count for the entire history."""
        self.approx_tokens = sum(len(json.dumps(m)) for m in self.history) // 4
    def reset_fresh(self, system_prompt: str, file_tree: str, behavior_rules: str = "") -> str:
        self.save()
        name = f"clean_{int(time.time())}"
        self._branch_fork[name] = 0
        first_content = system_prompt
        if behavior_rules:
            first_content += "\n\n" + behavior_rules
        first_content += "\n\n" + file_tree
        self.history = [
            {"role": "system", "content": first_content},
        ]
        self.current_branch_name = name
        self._labels = {}
        self.save()
        self.save("main")  # Also update main.json so next launch loads the fresh session
        self.recalculate_token_count()
        return name

    def list_branches(self) -> list[str]:
        return list(self._branch_fork.keys())

    def save(self, branch_name: str | None = None):
        name = branch_name or self.current_branch_name
        path = self.base_path / f"{name}.json"
        if not self.history and path.exists():
            return path
        data = {
            "branch_name": name,
            "fork_index": self._branch_fork.get(name, 0),
            "history": self.history,
            "labels": {str(k): v for k, v in self._labels.items()},
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        return path

    def load(self, branch_name: str) -> bool:
        path = self.base_path / f"{branch_name}.json"
        if not path.exists():
            return False
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            self.history = []
            self._labels = {}
            self.recalculate_token_count()
            return True
        self.history = data.get("history", [])
        self._branch_fork[branch_name] = data.get("fork_index", 0)
        self._labels = {int(k): v for k, v in data.get("labels", {}).items()}
        self.current_branch_name = branch_name
        self.recalculate_token_count()
        return True

    def export_html(self) -> str:
        lines = []
        for i, msg in enumerate(self.history):
            role = msg.get("role", "unknown")
            content = msg.get("content", "") or ""
            label = self._labels.get(i)
            tag = f" <b>[{label}]</b>" if label else ""
            if role == "user":
                lines.append(
                    f'<div class="msg user"><b>User{tag}:</b> {content}</div>'
                )
            elif role == "assistant":
                lines.append(
                    f'<div class="msg assistant"><b>Kyrex{tag}:</b> {content}</div>'
                )
            elif role == "tool":
                lines.append(
                    f'<div class="msg tool"><b>Tool ({msg.get("name", "")}){tag}:</b> {content}</div>'
                )
            elif role == "system":
                lines.append(
                    f'<div class="msg system"><b>System{tag}:</b> {content}</div>'
                )
        return f"""<html><head><style>
body {{ font-family: monospace; padding: 20px; background: #1e1e2e; color: #cdd6f4; }}
.msg {{ padding: 8px; margin: 4px 0; border-radius: 4px; white-space: pre-wrap; }}
.user {{ background: #313244; }}
.assistant {{ background: #45475a; }}
.tool {{ background: #1e1e2e; border: 1px solid #45475a; color: #a6adc8; }}
.system {{ background: #11111b; color: #6c7086; font-style: italic; }}
</style></head><body>{"".join(lines)}</body></html>"""


__all__ = ["TreeSessionManager"]
