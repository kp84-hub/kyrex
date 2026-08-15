import os
import json
import os
import time
import select
import subprocess
import tempfile
from pathlib import Path


_MCP_TIMEOUT = float(os.getenv("KYREX_MCP_TIMEOUT", "10"))
_DATA_FILE_EXTENSIONS = (".jsonl", ".json", ".db")


def _expanded_env_value(value: str) -> str:
    """Expand an environment value and prepare its directory when path-like."""
    expanded = os.path.expanduser(value)
    is_uri = "://" in expanded
    has_separator = os.path.sep in expanded or (os.path.altsep and os.path.altsep in expanded)
    has_data_extension = expanded.lower().endswith(_DATA_FILE_EXTENSIONS)
    if not is_uri and (has_separator or has_data_extension):
        os.makedirs(os.path.dirname(expanded) or ".", exist_ok=True)
    return expanded


def _recv_with_timeout(process, timeout=_MCP_TIMEOUT):
    """Read a line from process stdout with timeout. Returns None on timeout."""
    start = time.time()
    while time.time() - start < timeout:
        # Use select to check if data is available (works on Unix)
        ready, _, _ = select.select([process.stdout], [], [], 0.1)
        if ready:
            line = process.stdout.readline()
            if line:
                return line
        else:
            time.sleep(0.05)
    return None


class MCPServer:
    def __init__(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ):
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.process: subprocess.Popen | None = None
        self.tools: list[dict] = []
        self._req_id = 0
        self._disabled = False
        self._error = ""

    def _fail(self, msg: str):
        """Mark server as failed, cleaning up process."""
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except Exception:
                pass
            self.process = None
        self._disabled = True
        self._error = msg
        print(f"[!] MCP server '{self.name}' failed: {msg}")

    def start(self):
        if self.process:
            return
        process_env = os.environ.copy()
        process_env.update({key: _expanded_env_value(value) for key, value in self.env.items()})
        self.process = subprocess.Popen(
            [self.command] + self.args,
            env=process_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        try:
            self._send(
                "initialize",
                {
                    "protocolVersion": "0.1.0",
                    "capabilities": {},
                    "clientInfo": {"name": "kyrex", "version": "1.0.0"},
                },
            )
            resp = _recv_with_timeout(self.process, _MCP_TIMEOUT)
            if resp is None:
                self._fail(f"Initialization timed out after {_MCP_TIMEOUT}s")
                return
            init_result = json.loads(resp)
            if "error" in init_result:
                self._fail(f"Initialization error: {init_result['error']}")
                return
            
            self._send("tools/list", {})
            resp = _recv_with_timeout(self.process, _MCP_TIMEOUT)
            if resp is None:
                self._fail(f"tools/list timed out after {_MCP_TIMEOUT}s")
                return
            tool_result = json.loads(resp)
            if "error" in tool_result:
                self._fail(f"tools/list error: {tool_result['error']}")
                return
            self.tools = tool_result.get("result", {}).get("tools", [])
            self._disabled = False
        except (json.JSONDecodeError, OSError) as e:
            self._fail(f"Failed to start: {e}")

    def _send(self, method: str, params: dict):
        self._req_id += 1
        msg = {"jsonrpc": "2.0", "method": method, "params": params, "id": self._req_id}
        if self.process and self.process.stdin:
            self.process.stdin.write(json.dumps(msg) + "\n")
            self.process.stdin.flush()

    def _recv(self, timeout=_MCP_TIMEOUT) -> dict:
        if self.process and self.process.stdout:
            line = _recv_with_timeout(self.process, timeout)
            if line:
                return json.loads(line)
        return {}

    def call_tool(self, name: str, arguments: dict) -> dict:
        self._send("tools/call", {"name": name, "arguments": arguments})
        result = self._recv(timeout=_MCP_TIMEOUT * 2)
        if result is None or result == {}:
            return {"error": f"MCP tool '{name}' call timed out after {_MCP_TIMEOUT * 2}s"}
        return result.get("result", {})

    def stop(self):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process = None


class MCPManager:
    def __init__(self):
        self.servers: dict[str, MCPServer] = {}
        self.config_path = Path.home() / ".kyrex" / "mcp_servers.json"

    def _read_config(self) -> dict:
        if not self.config_path.exists():
            return {}
        with self.config_path.open(encoding="utf-8") as config_file:
            document = json.load(config_file)
        if not isinstance(document, dict):
            raise ValueError("MCP configuration must be a JSON object")
        config = {}
        for name, entry in document.items():
            if not isinstance(name, str) or not name:
                raise ValueError("MCP configuration contains an invalid server name")
            if not isinstance(entry, dict) or not isinstance(entry.get("command"), str) or not entry["command"]:
                raise ValueError(f"MCP configuration for {name!r} is invalid")
            args = entry.get("args", [])
            if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
                raise ValueError(f"MCP configuration arguments for {name!r} are invalid")
            env = entry.get("env", {})
            if not isinstance(env, dict) or not all(
                isinstance(key, str) and key and isinstance(value, str) for key, value in env.items()
            ):
                raise ValueError(f"MCP configuration environment for {name!r} is invalid")
            config[name] = {"command": entry["command"], "args": args, "env": env}
        return config

    def _load_config(self):
        config = self._read_config()
        previous = self.servers
        refreshed = {}
        for name, entry in config.items():
            existing = previous.get(name)
            if (
                existing
                and existing.command == entry["command"]
                and existing.args == entry["args"]
                and existing.env == entry["env"]
            ):
                refreshed[name] = existing
            else:
                if existing:
                    existing.stop()
                refreshed[name] = MCPServer(name, entry["command"], entry["args"], entry["env"])
        for name, server in previous.items():
            if name not in refreshed:
                server.stop()
        self.servers = refreshed
        return self.servers

    def refresh(self):
        """Reconcile the in-memory server set with persisted configuration."""
        return self._load_config()

    def _config_document(self, servers=None) -> dict:
        return {
            name: {
                "command": server.command,
                "args": list(server.args),
                **({"env": dict(server.env)} if server.env else {}),
            }
            for name, server in (servers if servers is not None else self.servers).items()
        }

    def _save_config(self):
        """Atomically persist configuration, leaving the old file intact on failure."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._config_document(), indent=2) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=f".{self.config_path.name}.", dir=self.config_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as config_file:
                config_file.write(payload)
                config_file.flush()
                os.fsync(config_file.fileno())
            os.replace(temporary, self.config_path)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def add(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ):
        candidate = MCPServer(name, command, args or [], env)
        previous = self.servers.get(name)
        self.servers[name] = candidate
        try:
            self._save_config()
        except Exception:
            if previous is None:
                self.servers.pop(name, None)
            else:
                self.servers[name] = previous
            raise
        if previous and previous is not candidate:
            previous.stop()

    def remove(self, name: str):
        previous = self.servers.pop(name, None)
        if previous is None:
            return
        try:
            self._save_config()
        except Exception:
            self.servers[name] = previous
            raise
        previous.stop()

    def start_all(self):
        self._load_config()
        for srv in self.servers.values():
            try:
                srv.start()
                if srv._disabled:
                    print(f"[!] MCP server '{srv.name}' failed: {srv._error}")
                else:
                    print(f"[*] MCP server '{srv.name}' started")
            except Exception as e:
                print(f"[!] MCP server '{srv.name}' failed to start: {e}")

    def stop_all(self):
        for srv in self.servers.values():
            try:
                srv.stop()
            except Exception:
                pass

    def test_connection(self, name: str) -> dict:
        """Start one configured server and verify initialize plus tools/list.

        This deliberately uses the existing MCPServer runtime and configuration
        owned by this manager; it does not create a second MCP process or config
        path. The returned record is safe to send to the UI.
        """
        if name not in self.servers:
            self._load_config()
        server = self.servers.get(name)
        if server is None:
            return {
                "success": False,
                "server": name,
                "tool_count": 0,
                "error": f"MCP server '{name}' is not configured",
            }

        # A connection test must exercise startup and the two MCP handshake
        # requests even when start_all previously attempted this server.
        server.stop()
        server._disabled = False
        server._error = ""
        try:
            server.start()
        except (OSError, BrokenPipeError, ValueError) as exc:
            server._fail(f"Failed to start: {exc}")

        if server._disabled:
            return {
                "success": False,
                "server": name,
                "tool_count": 0,
                "error": server._error or "MCP server failed to start",
            }
        return {
            "success": True,
            "server": name,
            "tool_count": len(server.tools),
            "error": "",
        }

    def get_tool_schemas(self) -> list:
        schemas = []
        for name, srv in self.servers.items():
            if srv._disabled:
                continue
            for tool in srv.tools:
                params = tool.get("inputSchema", {})
                schemas.append(
                    {
                        "type": "function",
                        "function": {
                            "name": f"mcp_{name}_{tool['name']}",
                            "description": tool.get("description", ""),
                            "parameters": {
                                "type": "object",
                                "properties": params.get("properties", {}),
                                "required": params.get("required", []),
                            },
                        },
                    }
                )
        return schemas

    def call_tool(self, full_name: str, arguments: dict) -> dict:
        if not full_name.startswith("mcp_"):
            return {"error": f"Invalid MCP tool name format: {full_name}"}
        
        rest = full_name[4:] # strip 'mcp_'
        # Iterate over servers to find the best match for the server name
        # We check from longest server name to shortest to avoid partial matches
        sorted_servers = sorted(self.servers.keys(), key=len, reverse=True)
        for server_name in sorted_servers:
            if rest.startswith(f"{server_name}_"):
                srv = self.servers[server_name]
                if srv._disabled:
                    return {"error": f"MCP server '{server_name}' is disabled: {srv._error}"}
                tool_name = rest[len(server_name) + 1:]
                return srv.call_tool(tool_name, arguments)
        
        return {"error": f"No MCP server found matching the prefix in '{full_name}'"}


__all__ = ["MCPManager", "MCPServer"]
