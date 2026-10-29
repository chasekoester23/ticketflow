"""Opt-in model candidates for PRIVATE, permissioned handwritten-ticket benchmarks.

The Gemini adapter is the primary reader in the production worker when
EXTRACTION_PROVIDER=gemini (the default). OpenAI is benchmark-only; Claude is
an explicit opt-in fallback. No paid requests occur until extract() is called.

Do not make assertions of handwriting accuracy from fake-client unit tests.
"""
from __future__ import annotations

from PIL import Image

from ..config import settings
from .extraction import (
    AnthropicExtractor,
    ExtractionError,
    PROMPT,
    _to_png_b64,
    parse_model_json,
)
from .validation import ExtractedTicket


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


class GeminiBenchmarkExtractor:
    """Google Gemini Generate Content with JSON response mode and shared prompt."""

    def __init__(self, *, api_key: str | None = None,
                 model: str = DEFAULT_MODELS["gemini"], client=None):
        self.model = model
        if client is not None:  # test fixture only; never used by the CLI
            self.client = client
            return
        key = api_key or settings.gemini_api_key
        if not key:
            raise ExtractionError("GEMINI_API_KEY is not set; cannot run extraction")
        try:
            from google import genai
        except ImportError as exc:
            raise ExtractionError(
                "Gemini SDK missing; run pip install -r requirements-dev.txt"
            ) from exc
        self.client = genai.Client(api_key=key, http_options={"timeout": 60000})

    def extract(self, image: Image.Image) -> tuple[ExtractedTicket, dict]:
        # Same max side as Claude/OpenAI so all model candidates see a cropped
        # image at comparable resolution rather than a downscaled full page.
        cropped = image.copy().convert("RGB")
        cropped.thumbnail((1800, 1800))
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=[PROMPT, cropped],
                config={"response_mime_type": "application/json"},
            )
        except Exception as exc:
            raise ExtractionError(
                f"Gemini benchmark request failed: {type(exc).__name__}"
            ) from exc
        return _validated_json(response.text)


def make_benchmark_extractor(provider: str, *, model: str | None = None):
    """Explicit provider selection only for the private evaluation CLI."""
    if provider not in DEFAULT_MODELS:
        raise ValueError(f"Unknown benchmark provider: {provider}")
    name = model or DEFAULT_MODELS[provider]
    if provider == "anthropic":
        return AnthropicExtractor(model=name)
    if provider == "openai":
        return OpenAIBenchmarkExtractor(model=name)
    return GeminiBenchmarkExtractor(model=name)
