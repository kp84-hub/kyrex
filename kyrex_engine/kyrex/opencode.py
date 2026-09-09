"""OpenCode gateway detection — single source of truth.

Both the setup wizard (kyrex.config) and the request layer
(kyrex.providers.openai_) must agree on what counts as an OpenCode target,
because the wizard decides whether to auto-generate x-opencode-session while
the provider decides whether to send it. Keeping the detection in one
dependency-free module (stdlib only, no SDK imports) means the two layers
can never drift apart, and the wizard's answer is always exactly what the
provider will check.
"""

import urllib.parse

# Header the OpenCode gateway uses to route requests to a conversation.
# For OpenCode setups the wizard generates and stores this automatically;
# the user never has to type it.
OPENCODE_SESSION_HEADER = "x-opencode-session"


def is_opencode_gateway(base_url: str | None) -> bool:
    """True when the request target is the OpenCode gateway host.

    OpenCode is configured as an OpenAI-compatible provider with the OpenCode
    base URL, so the only reliable OpenCode signal is the gateway host itself
    (not the provider name). Requests to other OpenAI-compatible endpoints
    (api.openai.com, OpenRouter, Ollama, custom hosts) and Anthropic must
    never receive OpenCode-specific headers.
    """
    if not base_url:
        return False
    try:
        host = urllib.parse.urlsplit(base_url).hostname or ""
    except Exception:
        return False
    host = host.lower()
    return host == "opencode.ai" or host.endswith(".opencode.ai")