import os
from .base import BaseProvider
from .openai_ import OpenAIProvider
from .anthropic import AnthropicProvider


_PROVIDER_MAP = {"openai": OpenAIProvider, "anthropic": AnthropicProvider}


def get_provider(
    name: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    extra_headers: dict | None = None,
) -> BaseProvider:
    name = (name or os.getenv("KYREX_PROVIDER") or os.getenv("PROVIDER") or "openai").lower()

    if not api_key:
        api_key = os.getenv("KYREX_API_KEY")
        if not api_key:
            if name == "openai":
                api_key = os.getenv("OPENAI_API_KEY")
            elif name == "anthropic":
                api_key = os.getenv("ANTHROPIC_API_KEY")
            elif name == "deepseek":
                api_key = os.getenv("DEEPSEEK_API_KEY")

    cls = _PROVIDER_MAP.get(name)
    if not cls:
        msg = f"Unknown provider '{name}'. Choose from: {list(_PROVIDER_MAP.keys())}"
        raise ValueError(msg)

    if name == "anthropic":
        return AnthropicProvider(api_key=api_key, base_url=base_url, extra_headers=extra_headers)

    base_url = base_url or os.getenv("KYREX_BASE_URL")
    return OpenAIProvider(api_key=api_key, base_url=base_url, extra_headers=extra_headers)


__all__ = ["BaseProvider", "OpenAIProvider", "AnthropicProvider", "get_provider"]
