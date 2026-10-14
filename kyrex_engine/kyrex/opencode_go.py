"""One Go profile; model families select their documented wire protocol.

Source: https://opencode.ai/v2/docs/console/go/#endpoints (2026-10-14).
The model list supplies suggestions, never an allowlist.
"""
from urllib.parse import urlsplit

GO_BASE = 'https://opencode.ai/inference/go/v1'
GO_MODELS = (
    'deepseek-v4.1-flash', 'gpt-6-luna', 'gemini-4-argon-contributor',
    'grok-4.7', 'grok-4.6', 'gpt-5.6-luna', 'claude-haiku-5-5',
    'glm-5.3-flash', 'glm-5.3', 'glm-5.2', 'kimi-k3', 'kimi-k2.7-code',
    'kimi-k2.6', 'longcat-2.0', 'deepseek-v4-pro', 'mimo-v2.6-flash',
    'mimo-v2.6-pro', 'mimo-v2.5', 'mimo-v2.5-pro', 'minimax-m3',
    'minimax-m2.7', 'muse-spark-1.3-contributor', 'muse-spark-1.2-contributor',
    'qwen3.8-max', 'qwen3.8-flash', 'qwen3.7-plus', 'hy4-preview', 'hy3',
    'space-bunny', 'longcat-2.5-preview-free', 'step-5-preview-free', 'cat-jam-free',
)

def go_base(base_url):
    try:
        parsed = urlsplit(base_url or '')
        port = parsed.port
    except ValueError:
        return None
    if (parsed.scheme != 'https' or parsed.hostname != 'opencode.ai'
            or port not in (None, 443) or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        return None
    path = parsed.path.rstrip('/')
    for suffix in ('/chat/completions', '/responses', '/messages', '/models'):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    return GO_BASE if path in {'/zen/go/v1', '/inference/go/v1', '/inference/go/openai/v1'} else None

def model_id(model):
    return model.removeprefix('opencode-go/')

def protocol(model):
    model = model_id(model)
    if model.startswith('gemini-'):
        return 'google'
    if model.startswith(('gpt-', 'grok-', 'muse-spark-')):
        return 'responses'
    if model.startswith(('claude-', 'minimax-', 'qwen')):
        return 'messages'
    return 'chat'

def suggestions(base_url, saved):
    return list(dict.fromkeys([*saved, *(GO_MODELS if go_base(base_url) else ())]))
