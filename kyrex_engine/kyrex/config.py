import os
import json
import re
from pathlib import Path


_PROVIDER_DEFAULTS = {
    "opencode": {
        "base_url": "https://opencode.ai/zen/go/v1",
        "provider_type": "openai",
        "label": "OpenCode (recommended)",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "provider_type": "openai",
        "label": "OpenRouter",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "provider_type": "openai",
        "label": "OpenAI",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com",
        "provider_type": "anthropic",
        "label": "Anthropic",
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
        # Merge with existing config instead of overwriting
        existing = {}
        if self.config_path.exists():
            try:
                existing = json.loads(self.config_path.read_text())
            except Exception:
                pass
        merged = {**existing, **data}
        self.config_path.write_text(json.dumps(merged, indent=2) + "\n")
        self._data = {k.lower(): v for k, v in merged.items()}

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

    def _fetch_model_list(self, provider: str, api_key: str, base_url: str | None) -> list[str] | None:
        """Try to fetch available models from the provider API. Returns None on failure."""
        try:
            if provider == "anthropic":
                from anthropic import Anthropic, APIError
                kwargs = {"api_key": api_key}
                if base_url:
                    kwargs["base_url"] = base_url
                client = Anthropic(**kwargs)
                try:
                    resp = client.models.list()
                    return sorted([m.id for m in resp.data])
                except (APIError, AttributeError, TypeError):
                    return None
            else:
                from openai import OpenAI, APIError
                kwargs = {"api_key": api_key}
                if base_url:
                    kwargs["base_url"] = base_url
                client = OpenAI(**kwargs)
                try:
                    raw = client.models.list()
                    all_ids = sorted([m.id for m in raw])
                    # Filter to likely chat models
                    chat_keywords = ("gpt", "claude", "deepseek", "gemini", "llama",
                                     "mistral", "qwen", "kimi", "command", "phi")
                    filtered = [m for m in all_ids if any(k in m.lower() for k in chat_keywords)]
                    return filtered[:50] if filtered else all_ids[:50]
                except (APIError, AttributeError, TypeError):
                    return None
        except Exception:
            return None

    def _pick_model_from_list(self, models: list[str], current: str) -> str:
        """Display a numbered list of models and let the user pick one."""
        C = '\033[96m'
        W = '\033[97m'
        N = '\033[0m'

        print(f"\n  {W}Available models:{N}")
        page_size = 20
        total = len(models)
        offset = 0

        while offset < total:
            page = models[offset:offset + page_size]
            for i, m in enumerate(page, offset + 1):
                marker = f" {C}>{N}" if m == current else "  "
                print(f"  {marker} {C}{i:3}.{N} {m}")
            offset += page_size

            if offset < total:
                more = input(f"  {W}Press Enter to show more ({offset}/{total}) or q to quit{N}: ").strip().lower()
                if more == 'q':
                    break
        print(f"  {C}{total + 1}.{N} {W}Enter a custom model name{N}")

        choice = input(f"\n  {W}Select model{N} (1-{total + 1}): ").strip()
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < total:
                return models[idx]
        return input(f"  {W}Model name{N}: ").strip()

    def setup_wizard(self):
        C = '\033[96m'
        W = '\033[97m'
        B = '\033[1m'
        G = '\033[92m'
        R = '\033[91m'
        Y = '\033[93m'
        N = '\033[0m'

        # -- Step 0: Welcome Banner --------------------------
        print()
        print(f"  {C}╭──────────────────────────────────────────────╮{N}")
        print(f"  {C}│{W}  ██╗  ██╗██╗   ██╗██████╗ ███████╗██╗  ██╗ {C}│{N}")
        print(f"  {C}│{W}  ██║ ██╔╝╚██╗ ██╔╝██╔══██╗██╔════╝╚██╗██╔╝ {C}│{N}")
        print(f"  {C}│{W}  █████╔╝  ╚████╔╝ ██████╔╝█████╗   ╚███╔╝  {C}│{N}")
        print(f"  {C}│{W}  ██╔═██╗   ╚██╔╝  ██╔══██╗██╔══╝   ██╔██╗  {C}│{N}")
        print(f"  {C}│{W}  ██║  ██╗   ██║   ██║  ██║███████╗██╔╝ ██╗ {C}│{N}")
        print(f"  {C}│{W}  ╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝ {C}│{N}")
        print(f"  {C}╰──────────────────────────────────────────────╯{N}")
        print(f"  {W}{B}         Terminal AI Agent — Setup{N}")
        print(f"  {C}──────────────────────────────────────────────────{N}")
        print()
        print(f"  {W}Welcome! Let's get Kyrex connected to an AI provider.{N}")
        print(f"  {W}This wizard walks you through each setting step by step.{N}")
        print()

        # Working config updated after each step so test_connection sees current values
        config_data = dict(self._data)

        # -- Step 1: Provider --------------------------------
        print(f"\n  {C}Step 1:{N} {B}Provider{N}")
        print(f"  {W}  Choose the AI service Kyrex will use.{N}")
        print()
        print(f"  {C}  1.{N} {W}OpenCode (recommended){N}  — https://opencode.ai/zen/go/v1")
        print(f"  {C}  2.{N} {W}OpenRouter{N}           — https://openrouter.ai/api/v1")
        print(f"  {C}  3.{N} {W}OpenAI{N}               — https://api.openai.com/v1")
        print(f"  {C}  4.{N} {W}Anthropic{N}            — https://api.anthropic.com")
        print(f"  {C}  5.{N} {W}Custom{N}               — manual configuration")
        print(f"  {C}  6.{N} {W}Ollama (local){N}       — http://localhost:11434/v1")
        print()

        provider_choice = input(f"  {W}Select option{N} (1-6) [{C}1{N}]: ").strip() or "1"

        is_ollama = provider_choice == "6"

        # Define preset configurations
        presets = {
            "1": {"label": "OpenCode (recommended)", "provider": "openai", "base_url": "https://opencode.ai/zen/go/v1"},
            "2": {"label": "OpenRouter", "provider": "openai", "base_url": "https://openrouter.ai/api/v1"},
            "3": {"label": "OpenAI", "provider": "openai", "base_url": "https://api.openai.com/v1"},
            "4": {"label": "Anthropic", "provider": "anthropic", "base_url": "https://api.anthropic.com"},
            "6": {"label": "Ollama (local)", "provider": "openai", "base_url": "http://localhost:11434/v1"},
        }

        skip_base_url = False
        if provider_choice in presets:
            preset = presets[provider_choice]
            provider = preset["provider"]
            base_url = preset["base_url"]
            skip_base_url = True
            print(f"  {G}+  {N} {W}Selected: {C}{preset['label']}{N}")
            print(f"  {W}  Base URL: {C}{base_url}{N}")
        elif provider_choice == "5":
            # Custom - ask for provider type and base URL
            current_provider = self.get_provider()
            provider_raw = input(f"\n  {W}Provider type [{C}anthropic{W}/{C}openai{W}]{N} ({C}{current_provider}{N}): ").strip().lower()
            provider = provider_raw or current_provider
            if provider not in ["openai", "anthropic"]:
                print(f"  {Y}Unknown provider '{provider}', defaulting to openai{N}")
                provider = "openai"
            skip_base_url = False
        else:
            # Default to OpenCode if invalid input
            preset = presets["1"]
            provider = preset["provider"]
            base_url = preset["base_url"]
            skip_base_url = True
            print(f"  {Y}Invalid choice, defaulting to OpenCode{N}")
            print(f"  {W}  Base URL: {C}{base_url}{N}")

        # Update working config with provider selection
        config_data["provider"] = provider
        if base_url:
            config_data["base_url"] = base_url
        else:
            config_data.pop("base_url", None)
        self._data = config_data

        # -- Step 2: Base URL --------------------------------
        if not skip_base_url:
            print()
            print(f"  {C}Step 2:{N} {B}API Base URL{N}")
            print(f"  {W}  The endpoint for API requests.{N}")
            print(f"  {W}  Change this if you're using a proxy, local server, or alternative provider.{N}")
            print()

            # For custom, suggest the default for the chosen provider
            if provider == "anthropic":
                suggested = "https://api.anthropic.com"
            else:
                suggested = ""  # No default for custom OpenAI-compatible

            if suggested:
                print(f"  {W}  Default: {C}{suggested}{N}")
            base_url_input = input(f"  {W}Base URL{N}" + (f" (Enter for {C}{suggested}{N}): " if suggested else ": ")).strip()
            base_url = base_url_input or suggested
        else:
            # Skip message for preset providers
            print(f"\n  {G}+  {N} {W}Base URL auto-filled, skipping manual entry.{N}")

        # -- Step 3: Authentication --------------------------
        api_key_env = None
        api_key = None
        if is_ollama:
            print()
            print(f"  {C}Step 3:{N} {B}Authentication{N}")
            print(f"  {W}  Ollama runs locally and requires no API key. Skipping...{N}")
            api_key = "ollama"
        else:
            print()
            print(f"  {C}Step 3:{N} {B}Authentication{N}")
            print(f"  {W}  Provide your API key. You can enter it directly, or use an{N}")
            print(f"  {W}  environment variable name (e.g. {C}MY_API_KEY{W}) that Kyrex will read at runtime.{N}")
            print()

            current_auth_env = config_data.get("api_key_env", "")
            current_direct = config_data.get("api_key", "") or ""
            masked = ""
            if current_auth_env:
                masked = f"${current_auth_env}"
            elif current_direct:
                masked = (current_direct[:8] + "...") if len(current_direct) > 12 else current_direct

            prompt = f"  {W}API Key or Env Var{N}"
            if masked:
                prompt += f" [{C}{masked}{N}]"
            prompt += ": "

            auth_input = input(prompt).strip()
            if auth_input:
                if re.match(r'^[A-Z_][A-Z0-9_]*$', auth_input):
                    api_key_env = auth_input
                    print(f"  {G}+{N} {W}Will read key from {C}${api_key_env}{W} at runtime{N}")
                else:
                    api_key = auth_input
                    mk = api_key[:8] + "..." if len(api_key) > 12 else api_key
                    print(f"  {G}+{N} {W}API key stored ({C}{mk}{W}){N}")
            else:
                if current_auth_env:
                    api_key_env = current_auth_env
                elif current_direct:
                    api_key = current_direct

        # Update working config with authentication
        if api_key_env:
            config_data["api_key_env"] = api_key_env
            config_data.pop("api_key", None)
        elif api_key:
            config_data["api_key"] = api_key
            config_data.pop("api_key_env", None)
        self._data = config_data

        # Resolve effective key for model fetching
        effective_key = api_key or (os.environ.get(api_key_env) if api_key_env else None)

        # -- Step 4: Model Selection -------------------------
        print()
        print(f"  {C}Step 4:{N} {B}Model{N}")
        print(f"  {W}  Select which model to use for conversations.{N}")
        if effective_key:
            print(f"  {W}  Fetching available models from your provider...{N}", end=" ")
            fetched = self._fetch_model_list(provider, effective_key, base_url)
            if fetched:
                print(f"{G}found {len(fetched)}{N}")
                current_model = self.get("model") or ""
                model = self._pick_model_from_list(fetched, current_model)
            else:
                print(f"{Y}unavailable{N}")
                print(f"  {W}  Could not retrieve model list. Enter model name manually.{N}")
                current_model = self.get("model") or ""
                fallback = "claude-sonnet-4-20250514" if provider == "anthropic" else ("llama3.2" if is_ollama else "gpt-4o")
                show = current_model or fallback
                model = input(f"  {W}Model{N} [{C}{show}{N}]: ").strip() or show
        else:
            print(f"  {Y}  No API key available to fetch models.{N}")
            current_model = self.get("model") or ""
            fallback = "claude-sonnet-4-20250514" if provider == "anthropic" else ("llama3.2" if is_ollama else "gpt-4o")
            show = current_model or fallback
            model = input(f"  {W}Model{N} [{C}{show}{N}]: ").strip() or show

        # Update working config with model selection
        config_data["model"] = model
        self._data = config_data

        # -- Step 5: Custom Headers (optional) ---------------
        print()
        print(f"  {C}Step 5:{N} {B}Custom Headers{N} {W}(optional){N}")
        print(f"  {W}  Some providers require additional headers (e.g. {C}x-api-key=...{W}).{N}")
        print(f"  {W}  Format: {C}Key=Value,Key2=Value2{N}")
        print()

        current_headers = self._data.get("headers", {})
        header_str = ",".join(f"{k}={v}" for k, v in current_headers.items())
        prompt = f"  {W}Headers{N}"
        if header_str:
            prompt += f" [{C}{header_str}{N}]"
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

        # Update working config with headers
        if headers:
            config_data["headers"] = headers
        else:
            config_data.pop("headers", None)
        self._data = config_data

        # -- Step 6: Connection Test -------------------------
        print()
        print(f"  {C}Step 6:{N} {B}Connection Test{N}")
        if is_ollama:
            ok = False
            print(f"  {W}  Skipping connection test for Ollama (local).{N}")
        else:
            print(f"  {W}  Kyrex will now attempt a test request to verify your configuration.{N}")

            ok, msg = self.test_connection()
            if ok:
                print(f"\n  {G}+  CONNECTION PASSED{N}")
                print(f"  {W}  Connected successfully to {C}{msg}{N}")
            else:
                print(f"\n  {R}x  CONNECTION FAILED{N}")
                print(f"  {W}  {R}{msg}{N}")
                if any(k in msg.lower() for k in ("api key", "unauthorized", "401", "403", "auth")):
                    print(f"  {Y}  -> Your API key may be invalid or expired.{N}")
                elif any(k in msg.lower() for k in ("connection", "timeout", "dns", "resolve")):
                    print(f"  {Y}  -> Could not reach the server. Check the base URL and your network.{N}")
                elif any(k in msg.lower() for k in ("model", "not found")):
                    print(f"  {Y}  -> The selected model may not be available.{N}")
                print(f"  {Y}  -> You can still save and fix these issues later with {C}kx --setup{N}")

        # -- Step 7: Confirmation Summary --------------------
        print()
        print(f"  {C}Step 7:{N} {B}Review & Save{N}")
        print()
        print(f"  {W}  Summary of your configuration:{N}")
        print(f"  {C}  -----------------------------------------{N}")
        print(f"  {W}  Provider     {C}{config_data.get('provider')}{N}")
        print(f"  {W}  Model        {C}{config_data.get('model')}{N}")
        print(f"  {W}  Base URL     {C}{config_data.get('base_url') or '(default)'}{N}")
        if api_key_env:
            status = f"{G}+{N}" if os.environ.get(api_key_env) else f"{Y}! not set{N}"
            print(f"  {W}  API Key      {C}${api_key_env}{N}  {status}")
        elif api_key:
            mk = api_key[:12] + "..." if len(api_key) > 16 else api_key
            print(f"  {W}  API Key      {C}{mk}{N}")
        if headers:
            hstr = ", ".join(f"{k}={v}" for k, v in headers.items())
            print(f"  {W}  Headers      {C}{hstr}{N}")
        print(f"  {W}  Config       {C}{self.config_path}{N}")
        if is_ollama:
            print(f"  {W}  Connection   {Y}- SKIPPED{N}")
        elif ok:
            print(f"  {W}  Connection   {G}+ PASS{N}")
        else:
            print(f"  {W}  Connection   {R}x FAIL{N}")
        print(f"  {C}  -----------------------------------------{N}")
        print()

        confirm = input(f"  {W}Save this configuration? ({C}y{W}/{C}N{W}){N}: ").strip().lower()
        if confirm == "y":
            final_data = {}
            for k in ("provider", "model", "base_url"):
                if config_data.get(k):
                    final_data[k] = config_data[k]
            if api_key_env:
                final_data["api_key_env"] = api_key_env
            elif api_key:
                final_data["api_key"] = api_key
            if headers:
                final_data["headers"] = headers
            self.save(final_data)
            print(f"\n  {G}+{N} {W}Configuration saved to{N}")
            print(f"    {C}{self.config_path}{N}")
            print(f"\n  {W}You're all set! Run {C}kx{W} to start using Kyrex.{N}")
        else:
            print(f"\n  {Y}Setup cancelled. No changes were saved.{N}")
            print(f"  {Y}Run {C}kx --setup{Y} again when you're ready.{N}")
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
