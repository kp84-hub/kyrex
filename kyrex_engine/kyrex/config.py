import os
import json
import re
from pathlib import Path


_PROVIDER_DEFAULTS = {
    "openai": {
        "base_url": None,
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com",
    },
}


def _find_workspace_root() -> Path | None:
    markers = [".px_sessions", ".git", ".vael_config"]
    cwd = Path.cwd().resolve()
    for p in [cwd] + list(cwd.parents):
        if any((p / m).exists() for m in markers):
            return p
    return None


class ConfigManager:
    def __init__(self, path: Path | None = None):
        if path:
            self.config_path = path
        else:
            project_cfg = Path(os.path.expanduser("~/.px/config.json"))
            workspace = _find_workspace_root()
            if workspace:
                candidate = workspace / ".px" / "config.json"
                if candidate.exists():
                    project_cfg = candidate
            self.config_path = project_cfg

        self._data: dict = {}

    def load(self) -> dict:
        cfg = {}
        if self.config_path.exists():
            try:
                cfg = json.loads(self.config_path.read_text())
            except (json.JSONDecodeError, OSError):
                cfg = {}
        self._data = {k.lower(): v for k, v in cfg.items()}
        return self._data

    def save(self, data: dict):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(data, indent=2) + "\n")
        self._data = {k.lower(): v for k, v in data.items()}

    def is_configured(self) -> bool:
        return bool(self.get_api_key())

    def get(self, key: str, default=None):
        key = key.lower()
        # Support both prefixes for transition
        for prefix in ["KYREX_", "VAEL_"]:
            env_key = f"{prefix}{key.upper()}"
            if env_key in os.environ:
                val = os.environ[env_key]
                return val.strip() if isinstance(val, str) else val

        provider = self.get_provider()

        if key in self._data:
            val = self._data[key]
            return val.strip() if isinstance(val, str) else val

        prefixed_key = f"{provider}_{key}"
        if prefixed_key in self._data:
            val = self._data[prefixed_key]
            return val.strip() if isinstance(val, str) else val

        if key == "api_key":
            if provider == "openai" and "OPENAI_API_KEY" in os.environ:
                return os.environ["OPENAI_API_KEY"].strip()
            if provider == "anthropic" and "ANTHROPIC_API_KEY" in os.environ:
                return os.environ["ANTHROPIC_API_KEY"].strip()

        if key == "base_url":
            if provider == "openai" and "OPENAI_BASE_URL" in os.environ:
                return os.environ["OPENAI_BASE_URL"].strip()

        return default

    def get_provider(self):
        val = (os.getenv("KYREX_PROVIDER") or os.getenv("VAEL_PROVIDER") or os.getenv("PROVIDER") or self._data.get("provider", "openai"))
        return val.lower().strip() if isinstance(val, str) else val.lower()

    def get_api_key(self) -> str | None:
        if os.getenv("KYREX_API_KEY"):
            return os.getenv("KYREX_API_KEY").strip()
        if os.getenv("VAEL_API_KEY"):
            return os.getenv("VAEL_API_KEY").strip()

        provider = self.get_provider()

        env_var = self._data.get("api_key_env")
        if env_var:
            token = os.environ.get(env_var)
            if not token:
                raise RuntimeError(
                    f"Environment variable '{env_var}' is not set. Run './kx --setup' to reconfigure."                )
            return token.strip()

        if self._data.get("api_key"):
            return self._data.get("api_key").strip()

        if f"{provider}_api_key" in self._data:
            return self._data[f"{provider}_api_key"].strip()

        val = self.get("api_key")
        return val.strip() if isinstance(val, str) else val

    def get_headers(self) -> dict:
        return self._data.get("headers", {})

    def test_connection(self) -> tuple[bool, str]:
        provider_name = self.get_provider()
        api_key = self.get_api_key()
        base_url = self.get("base_url")
        model = self.get("model")
        headers = self.get_headers()

        if not api_key:
            return False, "No API key configured."

        try:
            if provider_name == "anthropic":
                from anthropic import Anthropic
                kwargs = {"api_key": api_key}
                if base_url:
                    kwargs["base_url"] = base_url
                if headers:
                    kwargs["default_headers"] = headers
                client = Anthropic(**kwargs)
                client.messages.create(
                    model=model, max_tokens=10,
                    messages=[{"role": "user", "content": "ping"}],
                )
            else:
                from openai import OpenAI
                kwargs = {"api_key": api_key}
                if base_url:
                    kwargs["base_url"] = base_url
                if headers:
                    kwargs["default_headers"] = headers
                client = OpenAI(**kwargs)
                client.chat.completions.create(
                    model=model, messages=[{"role": "user", "content": "ping"}],
                    max_tokens=10,
                )
            return True, f"{provider_name} / {model}"
        except Exception as e:
            return False, str(e).split("\n")[0][:120]

    def setup_wizard(self):
        print()
        print("  ┌────────────────────────────────────────────┐")
        print("  │          Kyrex Setup                       │")
        print("  │                                            │")
        print("  │  Configure your API provider to get        │")
        print("  │  started with the Kyrex engine.            │")
        print("  └────────────────────────────────────────────┘")
        print()

        current_provider = self.get_provider()
        provider = input(f"  Provider [anthropic/openai] (default: {current_provider}): ").strip().lower() or current_provider
        if provider not in _PROVIDER_DEFAULTS:
            provider = "openai"

        defaults = _PROVIDER_DEFAULTS.get(provider, {})

        if provider == "anthropic":
            default_base_url = defaults.get("base_url", "https://api.anthropic.com")
        else:
            default_base_url = defaults.get("base_url")
        prompt = f"  Base URL (leave blank for default): "
        base_url = input(prompt).strip()
        if not base_url and provider == "anthropic":
            base_url = "https://api.anthropic.com"

        current_auth_env = self._data.get("api_key_env", "")
        current_direct = self._data.get("api_key", "") or ""
        masked = ""
        if current_auth_env:
            masked = f"${current_auth_env}"
        elif current_direct:
            if len(current_direct) > 12:
                masked = current_direct[:8] + "..."
            else:
                masked = current_direct

        prompt = f"  Auth method — enter API key directly, or enter an environment variable name (e.g. AI_PROXY_TOKEN)"
        if masked:
            prompt += f" [{masked}]"
        prompt += ": "
        auth_input = input(prompt).strip()

        api_key_env = None
        api_key = None
        if auth_input:
            if re.match(r'^[A-Z_][A-Z0-9_]*$', auth_input):
                api_key_env = auth_input
            else:
                api_key = auth_input
        else:
            if current_auth_env:
                api_key_env = current_auth_env
            elif current_direct:
                api_key = current_direct

        current_model = self.get("model") or ""
        default_model = ""
        if provider == "anthropic":
            default_model = "claude-sonnet-4-5-20250929"
        elif provider == "openai":
            default_model = "gpt-4o"
        show_model = current_model or default_model
        model = input(f"  Model (e.g. claude-sonnet-4-5, gpt-4o) [{show_model}]: ").strip() or show_model

        current_headers = self._data.get("headers", {})
        header_str = ",".join(f"{k}={v}" for k, v in current_headers.items())
        prompt = f"  Custom headers? (optional, format: Key=Value,Key2=Value2, leave blank to skip)"
        if header_str:
            prompt += f" [{header_str}]"
        prompt += ": "
        headers_input = input(prompt).strip()

        headers = {}
        if headers_input:
            for pair in headers_input.split(","):
                pair = pair.strip()
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    headers[k.strip()] = v.strip()
        elif header_str:
            headers = dict(current_headers)

        config_data = {
            "provider": provider,
            "base_url": base_url or None,
            "model": model,
            "headers": headers if headers else None,
        }

        if api_key_env:
            config_data["api_key_env"] = api_key_env
        elif api_key:
            config_data["api_key"] = api_key

        self._data = config_data

        print("\n  Testing connection...", end=" ")
        ok, msg = self.test_connection()
        if ok:
            print("OK")

            final_data = {k: v for k, v in config_data.items() if v is not None and v != ""}
            final_data["provider"] = config_data["provider"]
            final_data["model"] = config_data["model"]
            final_data["base_url"] = config_data["base_url"]
            if api_key_env:
                final_data["api_key_env"] = api_key_env
            elif api_key:
                final_data["api_key"] = api_key
            if headers:
                final_data["headers"] = headers

            self.save(final_data)
            print(f"  \u2713 Configuration saved to {self.config_path}")
        else:
            print(f"FAILED")
            print(f"  \u2717 {msg}")
            save_anyway = input("  Save anyway? (y/N): ").strip().lower()
            if save_anyway == "y":
                final_data = {k: v for k, v in config_data.items() if v is not None and v != ""}
                final_data["provider"] = config_data["provider"]
                final_data["model"] = config_data["model"]
                final_data["base_url"] = config_data["base_url"]
                if api_key_env:
                    final_data["api_key_env"] = api_key_env
                elif api_key:
                    final_data["api_key"] = api_key
                if headers:
                    final_data["headers"] = headers
                self.save(final_data)
                print(f"  \u2713 Configuration saved to {self.config_path}")
            else:
                print("  Setup cancelled.")
        print()

    def show_status(self):
        provider = self.get_provider()
        model = self.get("model")
        base_url = self.get("base_url")
        api_key_env = self._data.get("api_key_env")
        api_key = self.get_api_key()
        if api_key_env:
            masked = f"${api_key_env}"
        elif api_key:
            masked = api_key[:12] + "..." if len(api_key) > 16 else api_key
        else:
            masked = "not set"

        print(f"\n  Provider     {provider}")
        print(f"  Model        {model}")
        print(f"  Base URL     {base_url or '(default)'}")
        print(f"  API Key      {masked}")
        print(f"  Config file  {self.config_path}")
        print()

        ok, msg = self.test_connection()
        if ok:
            print(f"  Connection   \u2713 {msg}")
        else:
            print(f"  Connection   \u2717 {msg}")
        print()
()
