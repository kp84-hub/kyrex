import json
from pathlib import Path


class ExtensionTool:
    def __init__(self, name, description, handler, parameters=None):
        self.name = name
        self.description = description
        self.handler = handler
        self.parameters = parameters or {
            "type": "object",
            "properties": {},
            "required": [],
        }

    def to_openai_schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def execute(self, **kwargs):
        return self.handler(**kwargs)


class ExtensionRegistry:
    def __init__(self):
        self._tools: dict[str, ExtensionTool] = {}
        self._discovery_paths = [
            Path.home() / ".kyrex" / "extensions",
            Path(".px_extensions"),
        ]

    def register(self, name, description=None, parameters=None):
        def decorator(func):
            tool = ExtensionTool(
                name=name,
                description=description or func.__doc__ or name,
                handler=func,
                parameters=parameters,
            )
            self._tools[name] = tool
            return func

        return decorator

    def tool(self, name, description=None, parameters=None):
        return self.register(name, description, parameters)

    def get_tool(self, name):
        return self._tools.get(name)

    def get_all_tools(self):
        return list(self._tools.values())

    def to_openai_schemas(self):
        return [t.to_openai_schema() for t in self._tools.values()]

    def execute(self, tool_name, **kwargs):
        tool = self.get_tool(tool_name)
        if not tool:
            raise KeyError(f"Tool '{tool_name}' not found in registry")
        return tool.execute(**kwargs)

    def discover(self):
        import importlib.util
        import sys
        for d in self._discovery_paths:
            if not d.exists():
                continue
            for p in d.glob("*.py"):
                try:
                    module_name = f"px_ext_{p.stem}"
                    spec = importlib.util.spec_from_file_location(module_name, str(p))
                    if spec and spec.loader:
                        module = importlib.util.module_from_spec(spec)
                        sys.modules[module_name] = module
                        spec.loader.exec_module(module)
                        # The decorator in the module should have registered the tools
                        # But we also keep the fallback scan for backward compatibility if needed
                        for attr in dir(module):
                            obj = getattr(module, attr)
                            if callable(obj) and hasattr(obj, "_px_tool"):
                                tool = getattr(obj, "_px_tool")
                                self._tools[tool.name] = tool
                except Exception:
                    continue
        return self._tools

    def remove(self, name):
        self._tools.pop(name, None)


registry = ExtensionRegistry()
