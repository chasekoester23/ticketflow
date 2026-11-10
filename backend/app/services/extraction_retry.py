"""What to do when the reader fails: retry, slow down, or stop and say why.

Only temporary failures are retried: a rate limit (429), a server error (500, 502, 503,
504), a timeout or a dropped connection. A bad key, an unknown model or a request the
provider rejects is never retried, since trying again only costs time and money.

Messages name the kind of failure and the HTTP status, never the provider's own text,
which can quote the document.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass

RETRYABLE = {"rate_limited", "temporary", "timeout"}
SERVER_ERRORS = {500, 502, 503, 504}
_DELAY = re.compile(r"^\s*(\d+(?:\.\d+)?)s\s*$")


@dataclass(frozen=True)
class Failure:
    kind: str
    status: int | None = None
    retry_after: float | None = None


def _retry_after(exc: Exception) -> float | None:
    """The wait the provider asked for, from a Retry-After header or Google's RetryInfo."""
    response = getattr(exc, "response", None)
    header = getattr(response, "headers", None)
    if header is not None:
        try:
            value = header.get("retry-after")
            if value is not None:
                return max(0.0, float(value))
        except (TypeError, ValueError):
            pass
    details = getattr(exc, "details", None)
    error = details.get("error", details) if isinstance(details, dict) else None
    for item in (error or {}).get("details", []) if isinstance(error, dict) else []:
        if isinstance(item, dict) and str(item.get("@type", "")).endswith("RetryInfo"):
            match = _DELAY.match(str(item.get("retryDelay", "")))
            if match:
                return float(match.group(1))
    return None


def classify(exc: Exception) -> Failure:
    status = getattr(exc, "code", None)
    if isinstance(status, int) and 100 <= status <= 599 and not isinstance(exc, OSError):
        if status == 429:
            return Failure("rate_limited", status, _retry_after(exc))
        if status in SERVER_ERRORS or status >= 500:
            return Failure("temporary", status, _retry_after(exc))
        if status in (401, 403, 404):
            return Failure("configuration", status)
        return Failure("rejected", status)
    name = type(exc).__name__
    if isinstance(exc, TimeoutError) or "Timeout" in name:
        return Failure("timeout")
    if isinstance(exc, ConnectionError) or name in {"ConnectError", "ReadError", "WriteError", "RemoteProtocolError",
                                                    "NetworkError", "TransportError", "ProtocolError"}:
        return Failure("timeout")
    return Failure("unexpected")


def backoff_seconds(attempt: int, *, base: float, cap: float, rand) -> float:
    """Exponential, with half of each wait random so parallel tickets don't retry in step."""
    ceiling = min(cap, base * (2 ** max(0, attempt - 1)))
    return ceiling / 2 + rand() * ceiling / 2


def message(failure: Failure, *, attempts: int, model: str) -> str:
    tries = f"Tried {attempts} time{'s' if attempts != 1 else ''}."
    code = f" ({failure.status})" if failure.status else ""
    if failure.kind == "rate_limited":
        return (f"Gemini request failed: the account is sending tickets faster than Gemini allows{code}. {tries} "
                "Retry the batch in a minute; tickets already read are kept. A lower EXTRACTION_CONCURRENCY also helps.")
    if failure.kind == "temporary":
        return f"Gemini request failed: a temporary problem on Gemini's side{code}. {tries} Retry the batch; tickets already read are kept."
    if failure.kind == "timeout":
        return f"Gemini request failed: no answer in time, or the connection dropped. {tries} Check the internet connection, then retry the batch."
    if failure.kind == "configuration":
        if failure.status == 404:
            return f"Gemini request failed: Gemini doesn't recognise the model '{model}' (404). Check GEMINI_MODEL in the backend settings, then retry. Not retried."
        return f"Gemini request failed: Gemini refused the API key{code}. Check GEMINI_API_KEY in the backend settings, then retry. Not retried."
    if failure.kind == "rejected":
        return (f"Gemini request failed: Gemini rejected this request{code}, often an image it can't use. "
                "Enter this ticket by hand. Not retried.")
    return "Gemini request failed: an unexpected error. Retry the batch, or enter this ticket by hand."


class RateGate:
    """Shared by every ticket in a batch: once Gemini says slow down, all of them wait."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._until = 0.0

    def hold(self, seconds: float, now: float) -> None:
        with self._lock:
            self._until = max(self._until, now + seconds)

    def remaining(self, now: float) -> float:
        with self._lock:
            return max(0.0, self._until - now)
