import os
import json
import time
import select
import subprocess
from pathlib import Path


_MCP_TIMEOUT = float(os.getenv("VAEL_MCP_TIMEOUT", "10"))


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
    def __init__(self, name: str, command: str, args: list[str] | None = None):
        self.name = name
        self.command = command
        self.args = args or []
        self.process: subprocess.Popen | None = None
        self.tools: list[dict] = []
        self._req_id = 0

    def start(self):
        if self.process:
            return
        self.process = subprocess.Popen(
            [self.command] + self.args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        try:
            self._send("initialize", {"protocolVersion": "0.1.0", "capabilities": {}})
            resp = _recv_with_timeout(self.process, _MCP_TIMEOUT)
            if resp is None:
                raise TimeoutError(f"MCP server '{self.name}' initialization timed out after {_MCP_TIMEOUT}s")
            init_result = json.loads(resp)
            if "error" in init_result:
                raise RuntimeError(f"MCP server '{self.name}' initialization error: {init_result['error']}")
            
            self._send("tools/list", {})
            resp = _recv_with_timeout(self.process, _MCP_TIMEOUT)
            if resp is None:
                raise TimeoutError(f"MCP server '{self.name}' tools/list timed out after {_MCP_TIMEOUT}s")
            tool_result = json.loads(resp)
            if "error" in tool_result:
                raise RuntimeError(f"MCP server '{self.name}' tools/list error: {tool_result['error']}")
            self.tools = tool_result.get("result", {}).get("tools", [])
        except (json.JSONDecodeError, OSError) as e:
            self.process.terminate()
            self.process.wait(timeout=3)
            self.process = None
            raise RuntimeError(f"MCP server '{self.name}' failed to start: {e}")

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

    def _load_config(self):
        if self.config_path.exists():
            with open(self.config_path) as f:
                for name, cfg in json.load(f).items():
                    if name not in self.servers:
                        self.servers[name] = MCPServer(
                            name, cfg["command"], cfg.get("args", [])
                        )

    def _save_config(self):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        config = {}
        for name, srv in self.servers.items():
            config[name] = {"command": srv.command, "args": srv.args}
        with open(self.config_path, "w") as f:
            json.dump(config, f, indent=2)

    def add(self, name: str, command: str, args: list[str] | None = None):
        self.servers[name] = MCPServer(name, command, args or [])
        self._save_config()

    def remove(self, name: str):
        srv = self.servers.pop(name, None)
        if srv:
            srv.stop()
        self._save_config()

    def start_all(self):
        self._load_config()
        for srv in self.servers.values():
            try:
                srv.start()
                print(f"[*] MCP server '{srv.name}' started")
            except Exception as e:
                print(f"[!] MCP server '{srv.name}' failed to start: {e}")

    def stop_all(self):
        for srv in self.servers.values():
            try:
                srv.stop()
            except Exception:
                pass

    def get_tool_schemas(self) -> list:
        schemas = []
        for name, srv in self.servers.items():
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
            raise KeyError(f"Invalid MCP tool name format: {full_name}")
        
        rest = full_name[4:] # strip 'mcp_'
        # Iterate over servers to find the best match for the server name
        # We check from longest server name to shortest to avoid partial matches
        sorted_servers = sorted(self.servers.keys(), key=len, reverse=True)
        for server_name in sorted_servers:
            if rest.startswith(f"{server_name}_"):
                tool_name = rest[len(server_name) + 1:]
                return self.servers[server_name].call_tool(tool_name, arguments)
        
        raise KeyError(f"No MCP server found matching the prefix in '{full_name}'")


__all__ = ["MCPManager", "MCPServer"]
