"""Opt-in model candidates for PRIVATE, permissioned handwritten-ticket benchmarks.

The Gemini adapter is the primary reader in the production worker when
EXTRACTION_PROVIDER=gemini (the default). OpenAI is benchmark-only; Claude is
an explicit opt-in fallback. No paid requests occur until extract() is called.

Do not make assertions of handwriting accuracy from fake-client unit tests.
"""
from __future__ import annotations

import io
import logging
import random
import threading
import time

from PIL import Image

from ..config import settings
from .extraction import (
    AnthropicExtractor,
    ExtractionError,
    PROMPT,
    _to_png_b64,
    parse_model_json,
)
from .extraction_retry import RETRYABLE, RateGate, backoff_seconds, classify, message
from .validation import ExtractedTicket

logger = logging.getLogger(__name__)


DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5-5",
    "openai": "gpt-5.4-mini",
    "gemini": "gemini-3.1-pro-preview",
}


def _validated_json(text: str | None) -> tuple[ExtractedTicket, dict]:
    if not text:
        raise ExtractionError("Vision provider returned no text")
    raw = parse_model_json(text)
    try:
        fields = {name: raw.get(name) for name in ExtractedTicket.model_fields}
        return ExtractedTicket.model_validate(fields), raw
    except Exception as exc:
        raise ExtractionError("Vision provider returned invalid ticket fields") from exc


class OpenAIBenchmarkExtractor:
    """OpenAI Responses API with a cropped PNG and shared TicketFlow prompt."""

    def __init__(self, *, api_key: str | None = None,
                 model: str = DEFAULT_MODELS["openai"], client=None):
        self.model = model
        if client is not None:  # test fixture only; never used by the CLI
            self.client = client
            return
        key = api_key or settings.openai_api_key
        if not key:
            raise ExtractionError("OPENAI_API_KEY is not set; no benchmark calls made")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ExtractionError(
                "OpenAI benchmark SDK missing; install requirements-benchmark.txt"
            ) from exc
        self.client = OpenAI(api_key=key, timeout=60.0, max_retries=2)

    def extract(self, image: Image.Image) -> tuple[ExtractedTicket, dict]:
        data_url = "data:image/png;base64," + _to_png_b64(image)
        try:
            response = self.client.responses.create(
                model=self.model,
                max_output_tokens=600,
                input=[{
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": PROMPT},
                        {"type": "input_image", "image_url": data_url, "detail": "high"},
                    ],
                }],
            )
        except Exception as exc:
            raise ExtractionError(
                f"OpenAI benchmark request failed: {type(exc).__name__}"
            ) from exc
        return _validated_json(response.output_text)


def encode_image(image: Image.Image, *, fmt: str, max_side: int, quality: int = 92) -> tuple[bytes, str]:
    """The crop as the reader receives it. png and webp_lossless keep every pixel."""
    picture = image.copy().convert("RGB")
    picture.thumbnail((max_side, max_side))
    buffer = io.BytesIO()
    if fmt == "jpeg":
        # 4:4:4 keeps thin pen strokes from blurring into the paper.
        picture.save(buffer, format="JPEG", quality=quality, subsampling=0, optimize=True)
        return buffer.getvalue(), "image/jpeg"
    if fmt == "webp_lossless":
        picture.save(buffer, format="WEBP", lossless=True, quality=60, method=2)
        return buffer.getvalue(), "image/webp"
    picture.save(buffer, format="PNG")
    return buffer.getvalue(), "image/png"


class GeminiBenchmarkExtractor:
    """The live Gemini reader: JSON response mode, the shared prompt, and careful retries.

    Safe to share between threads: every request is independent, and the only shared
    state is the rate-limit cool-down and the stop after a configuration failure.
    """

    def __init__(self, *, api_key: str | None = None,
                 model: str = DEFAULT_MODELS["gemini"], client=None,
                 sleep=time.sleep, clock=time.monotonic, rand=random.random,
                 max_attempts: int | None = None, image_format: str | None = None):
        self.model = model
        self.max_attempts = max_attempts or settings.extraction_max_attempts
        self.image_format = image_format or settings.gemini_image_format
        self._sleep, self._clock, self._rand = sleep, clock, rand
        self._gate = RateGate()
        self._halted: str | None = None
        self._halt_lock = threading.Lock()
        if client is not None:  # test fixture only; never used by the CLI
            self.client = client
            return
        key = api_key or settings.gemini_api_key
        if not key:
            raise ExtractionError("GEMINI_API_KEY is not set; cannot run extraction", kind="configuration")
        try:
            from google import genai
        except ImportError as exc:
            raise ExtractionError(
                "Gemini SDK missing; run pip install -r requirements-dev.txt", kind="configuration"
            ) from exc
        self.client = genai.Client(api_key=key, http_options={"timeout": int(settings.extraction_timeout_seconds * 1000)})

    def _contents(self, image: Image.Image):
        data, mime = encode_image(image, fmt=self.image_format, max_side=settings.gemini_max_image_side,
                                  quality=settings.gemini_jpeg_quality)
        from google.genai import types
        return [PROMPT, types.Part.from_bytes(data=data, mime_type=mime)], len(data)

    def _config(self) -> dict:
        config: dict = {"response_mime_type": "application/json"}
        if settings.gemini_thinking_level:
            config["thinking_config"] = {"thinking_level": settings.gemini_thinking_level}
        return config

    def extract(self, image: Image.Image) -> tuple[ExtractedTicket, dict]:
        if self._halted:
            # One refused key means they'll all be refused: no more calls this batch.
            raise ExtractionError(self._halted, kind="configuration", attempts=0)
        contents, image_bytes = self._contents(image)
        started = self._clock()
        attempt = 0
        while True:
            attempt += 1
            wait = self._gate.remaining(self._clock())
            if wait:
                self._sleep(wait)
            try:
                response = self.client.models.generate_content(model=self.model, contents=contents, config=self._config())
                break
            except Exception as exc:  # classified below; never shown as OCR
                failure = classify(exc)
                if failure.kind not in RETRYABLE or attempt >= self.max_attempts:
                    text = message(failure, attempts=attempt, model=self.model)
                    if failure.kind == "configuration":
                        with self._halt_lock:
                            self._halted = self._halted or text
                    logger.warning("extraction_failed", extra={"kind": failure.kind, "status": failure.status, "attempts": attempt})
                    raise ExtractionError(text, kind=failure.kind, status=failure.status, attempts=attempt) from exc
                delay = backoff_seconds(attempt, base=settings.extraction_retry_base_seconds,
                                        cap=settings.extraction_retry_max_seconds, rand=self._rand)
                if failure.retry_after is not None:
                    delay = min(settings.extraction_retry_max_seconds, max(delay, failure.retry_after))
                logger.warning("extraction_retry", extra={"kind": failure.kind, "status": failure.status,
                                                          "attempt": attempt, "wait_seconds": round(delay, 2)})
                if failure.kind == "rate_limited":
                    self._gate.hold(delay, self._clock())
                else:
                    self._sleep(delay)
        ticket, raw = _validated_json(response.text)
        usage = getattr(response, "usage_metadata", None)
        raw = {**raw, "_ticketflow": {
            "model": self.model, "attempts": attempt, "seconds": round(self._clock() - started, 3),
            "image_format": self.image_format, "image_bytes": image_bytes,
            "prompt_tokens": getattr(usage, "prompt_token_count", None),
            "output_tokens": getattr(usage, "candidates_token_count", None),
            "thinking_tokens": getattr(usage, "thoughts_token_count", None),
        }}
        return ticket, raw


def resolve_model(provider: str, model: str | None = None) -> str:
    """An explicit --model wins; Gemini otherwise uses the SAME model the app
    is configured with (GEMINI_MODEL), so the benchmark measures what the
    worker actually runs. Other providers keep their documented defaults."""
    if provider not in DEFAULT_MODELS:
        raise ValueError(f"Unknown benchmark provider: {provider}")
    if model:
        return model
    if provider == "gemini" and settings.gemini_model:
        return settings.gemini_model
    return DEFAULT_MODELS[provider]


def make_benchmark_extractor(provider: str, *, model: str | None = None):
    """Explicit provider selection only for the private evaluation CLI."""
    name = resolve_model(provider, model)
    if provider == "anthropic":
        return AnthropicExtractor(model=name)
    if provider == "openai":
        return OpenAIBenchmarkExtractor(model=name)
    return GeminiBenchmarkExtractor(model=name)
