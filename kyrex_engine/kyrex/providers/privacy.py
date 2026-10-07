"""Local credential filtering at the model-request boundary.

This reduces accidental credential disclosure; it does not anonymize people,
email bodies, locations, or spending patterns. Authentication headers are
deliberately outside this filter: the provider still needs its own API key.
"""
from __future__ import annotations

import json
import os
import re

REDACTED = "[REDACTED_SECRET]"
_FIELD = r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|private[_-]?key|password|passwd|authorization|cookie|secret|token)"
_SENSITIVE_KEY = re.compile(rf"(?:^|[_-]){_FIELD}$", re.I)
_ASSIGNMENT = re.compile(
    rf"\b((?:[\w]+[_-])?{_FIELD})\b(\s*[:=]\s*)(\"[^\"\n]*\"|'[^'\n]*'|[^\s,;}}&<>]+)", re.I)
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----", re.S)
_BEARER = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{16,}", re.I)
_TOKEN = re.compile(r"\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b")
_COOKIE_HEADER = re.compile(r"(?im)^((?:set-)?cookie\s*:\s*)[^\r\n]+")
_URL_AUTH = re.compile(r"([a-z][a-z0-9+.-]*://)[^/\s@]+@", re.I)
_SIGNED_URL = re.compile(r"([?&](?:x-amz-signature|x-amz-security-token|x-goog-signature|sig)=)[^&\s\"'<>]+", re.I)


class SecretFilter:
    def __init__(self, secrets=()):
        # Capture known credentials without reading files or making requests.
        values = [v for k, v in os.environ.items()
                  if _SENSITIVE_KEY.search(k) and len(v) >= 8]
        values.extend(v for v in secrets if isinstance(v, str) and len(v) >= 8)
        self._secrets = tuple(sorted(set(values), key=len, reverse=True))

    def text(self, value: str) -> str:
        text = _PRIVATE_KEY.sub(REDACTED, value)
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        text = _BEARER.sub(lambda m: m[1] + " " + REDACTED, text)
        text = _TOKEN.sub(REDACTED, text)
        text = _COOKIE_HEADER.sub(lambda m: m[1] + REDACTED, text)
        text = _URL_AUTH.sub(lambda m: m[1] + REDACTED + "@", text)
        text = _SIGNED_URL.sub(lambda m: m[1] + REDACTED, text)
        # Keep JSON payloads valid, including function-call argument strings.
        if text.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, (dict, list)):
                    filtered = self.value(parsed)
                    return text if filtered == parsed else json.dumps(filtered, ensure_ascii=False)
            except (ValueError, TypeError):
                pass
        def replace(match):
            original = match[3]
            quote = original[0] if original.startswith(("\"", "'")) else ""
            return match[1] + match[2] + quote + REDACTED + quote
        return _ASSIGNMENT.sub(replace, text)

    def value(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, dict):
            return {key: (REDACTED if _SENSITIVE_KEY.search(str(key)) and item
                          else self.value(item)) for key, item in value.items()}
        return value

    def messages(self, messages: list) -> list:
        # Protocol identifiers and names are not user content. Preserve them.
        result = []
        for message in messages:
            copy = dict(message)
            for field in ("content", "reasoning_content", "reasoning"):
                if field in copy:
                    copy[field] = self.value(copy[field])
            if copy.get("tool_calls"):
                copy["tool_calls"] = [
                    {**call, "function": {**call["function"],
                      "arguments": self.text(call["function"].get("arguments") or "{}")}}
                    for call in copy["tool_calls"]
                ]
            result.append(copy)
        if result == messages:
            return messages
        if any(REDACTED in str(message.get("content") or "") for message in result):
            notice = ("Sensitive credentials were replaced with [REDACTED_SECRET]. "
                      "Do not guess credentials or use that marker in tool calls. "
                      "Use tools with host-managed authentication instead.")
            system = next((item for item in result if item.get("role") == "system"
                           and isinstance(item.get("content"), str)), None)
            if system is not None:
                system["content"] += "\n" + notice
            else:
                result.insert(0, {"role": "system", "content": notice})
        return result

    def tools(self, tools):
        # Tool schemas may contain a property named "password". Keep the
        # schema itself intact; sanitize descriptive strings and known values.
        def strings(value):
            if isinstance(value, str):
                return self.text(value)
            if isinstance(value, list):
                return [strings(item) for item in value]
            if isinstance(value, dict):
                return {key: strings(item) for key, item in value.items()}
            return value
        filtered = strings(tools)
        return tools if filtered == tools else filtered


def safe_provider_error(error: Exception) -> str:
    """Diagnose failures without echoing upstream bodies, URLs, or prompts."""
    status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(getattr(error, "response", None), "status_code", None)
    explanations = {
        400: "Provider rejected the request. Check the model ID and provider settings.",
        401: "Provider authentication failed. Check the API key.",
        403: "Provider access was denied. Check account and model permissions.",
        404: "Provider endpoint or model was not found. Check the API URL and model ID.",
        429: "Provider usage or rate limit reached. Try again later.",
    }
    if isinstance(status, int) and 400 <= status <= 599:
        return explanations.get(status, "Provider request failed. Try again later.") + f" (HTTP {status})"
    kind = type(error).__name__
    if kind in {"APITimeoutError", "TimeoutError", "ReadTimeout", "ConnectTimeout"}:
        return "Provider request timed out. Try again."
    if kind in {"APIConnectionError", "ConnectionError", "ConnectError"}:
        return "Could not connect to the provider. Check its API URL and try again."
    # Only these fixed, locally generated stream diagnoses are safe to echo.
    if isinstance(error, RuntimeError) and error.args in (
        ("OpenCode Responses stream ended without completion",),
        ("OpenCode Responses request response.failed",),
        ("OpenCode Responses request response.incomplete",),
    ):
        return error.args[0]
    return "Provider request failed. Check provider settings and try again."
