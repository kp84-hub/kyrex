import os
from .base import BaseProvider

def get_provider(
    name: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    extra_headers: dict | None = None,
) -> BaseProvider:
    name = (name or os.getenv("KYREX_PROVIDER") or os.getenv("PROVIDER") or "openai").lower()
    if not api_key:
        api_key = os.getenv("KYREX_API_KEY")
        if api_key:
            api_key = api_key.strip()
        if not api_key:
            if name == "openai":
                api_key = os.getenv("OPENAI_API_KEY")
            elif name == "anthropic":
                api_key = os.getenv("ANTHROPIC_API_KEY")
            elif name == "deepseek":
                api_key = os.getenv("DEEPSEEK_API_KEY")
            if api_key:
                api_key = api_key.strip()
    if name == "anthropic":
        from .anthropic import AnthropicProvider
        return AnthropicProvider(api_key=api_key, base_url=base_url, extra_headers=extra_headers)
    from .openai_ import OpenAIProvider
    base_url = base_url or os.getenv("KYREX_BASE_URL")
    return OpenAIProvider(api_key=api_key, base_url=base_url, extra_headers=extra_headers)

__all__ = ["BaseProvider", "get_provider"]
