"""Turn AI-provider failures into specific, safe advice for the operator.

Messages from Google/Anthropic can echo request details; everything shown
is passed through ``redact`` so an API key is never printed.
"""
from __future__ import annotations

import re

_KEY_PATTERNS = (
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),          # Google API keys
    re.compile(r"sk-[0-9A-Za-z_\-]{16,}"),           # Anthropic/OpenAI style keys
    re.compile(r"(?i)(key=)[^&\s]+"),                 # key=... in URLs
)


def redact(text: object, secret: str | None = None) -> str:
    result = str(text)
    if secret:
        result = result.replace(secret, "[redacted]")
    for pattern in _KEY_PATTERNS:
        result = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "[redacted]", result)
    return result


def model_name_problem(model: str | None) -> str | None:
    """Catch configuration typos before spending a request."""
    if not model or not model.strip():
        return "GEMINI_MODEL is empty in backend/.env."
    if "=" in model:
        return (f"GEMINI_MODEL looks malformed ({model!r}). The line should read "
                "GEMINI_MODEL=model-name with the name written only once.")
    if any(ch in model for ch in " \t\"'"):
        return f"GEMINI_MODEL contains spaces or quotes ({model!r}); remove them."
    return None


def explain_failure(exc: BaseException, *, model: str, secret: str | None = None) -> str:
    """One actionable sentence plus the provider's (redacted) status details."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    status = getattr(exc, "status", None) or ""
    message = getattr(exc, "message", None) or str(exc)
    lowered = str(message).lower()
    if code == 429 and "limit: 0" in lowered:
        advice = (f"Your plan has no quota for '{model}' (limit 0; often a paid-only model). "
                  "Enable billing, or set GEMINI_MODEL to a model your plan includes "
                  "(see: python scripts/check_api_key.py --list-models).")
    elif code == 429:
        advice = ("Rate limit or daily quota reached. Wait and try again; for benchmarks "
                  "add --pause-seconds to space out requests.")
    elif code == 404:
        advice = (f"Model '{model}' was not found or is no longer offered to your account. "
                  "Check GEMINI_MODEL (see --list-models).")
    elif code == 400 and "api key" in lowered:
        advice = ("The provider rejected the API key. Re-copy it into backend/.env with no "
                  "spaces or quotes after the '='.")
    elif code in (401, 403):
        advice = "The key is not permitted to use this API or model. Check the key's project and API access."
    elif isinstance(code, int) and code >= 500:
        advice = "The provider is temporarily unavailable or overloaded. Wait a minute and retry."
    else:
        advice = "The request failed. Check the key, model name, billing and quota."
    detail = " ".join(str(part) for part in (code, status) if part)
    summary = redact(message, secret).strip().splitlines()[0][:300] if message else ""
    return f"{advice}\n  Details: {detail or type(exc).__name__}: {summary}"
