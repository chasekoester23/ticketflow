"""Offline fake-client coverage for optional handwriting model candidates.

These tests check transport shape, strict parsing, and fail-closed behavior.
They provide ZERO evidence of actual real-handwriting accuracy.
"""
from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from PIL import Image

from app.config import settings
from app.services.benchmark_providers import (
    GeminiBenchmarkExtractor, OpenAIBenchmarkExtractor, make_benchmark_extractor,
)
from app.services.extraction import ExtractionError
from .sample_data import TICKETS


class DummyOpenAI:
    def __init__(self, response_text):
        self.text = response_text
        self.calls = []
        self.responses = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=self.text)


class DummyGemini:
    def __init__(self, response_text):
        self.text = response_text
        self.calls = []
        self.models = self

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(text=self.text)


def _truth_json():
    import json
    return json.dumps(TICKETS[0])


def test_openai_candidate_uses_one_cropped_png_and_same_six_field_prompt():
    client = DummyOpenAI(_truth_json())
    extractor = OpenAIBenchmarkExtractor(model="gpt-5.4-mini", client=client)
    image = Image.new("RGB", (2200, 2100), "white")
    ticket, raw = extractor.extract(image)
    assert ticket.ticket_number == TICKETS[0]["ticket_number"]
    assert raw == TICKETS[0]
    assert len(client.calls) == 1
    req = client.calls[0]
    assert req["model"] == "gpt-5.4-mini"
    parts = req["input"][0]["content"]
    assert "Net Tons" in parts[0]["text"]
    assert parts[1]["detail"] == "high"
    prefix = "data:image/png;base64,"
    assert parts[1]["image_url"].startswith(prefix)
    assert base64.b64decode(parts[1]["image_url"][len(prefix):]).startswith(b"\x89PNG")


def test_gemini_candidate_uses_json_response_and_resizes_to_shared_1800px_cap():
    client = DummyGemini(_truth_json())
    extractor = GeminiBenchmarkExtractor(model="gemini-3.1-pro-preview", client=client)
    original = Image.new("RGB", (2500, 2200), "white")
    ticket, raw = extractor.extract(original)
    assert ticket.tonnage == TICKETS[0]["tonnage"]
    assert raw == TICKETS[0]
    assert len(client.calls) == 1
    req = client.calls[0]
    assert req["model"] == "gemini-3.1-pro-preview"
    assert req["config"]["response_mime_type"] == "application/json"
    assert "Net Tons" in req["contents"][0]
    assert max(req["contents"][1].size) == 1800
    assert original.size == (2500, 2200)


@pytest.mark.parametrize("candidate", [OpenAIBenchmarkExtractor, GeminiBenchmarkExtractor])
def test_candidate_malformed_json_is_a_visible_failure(candidate):
    dummy = DummyOpenAI("not json") if candidate is OpenAIBenchmarkExtractor else DummyGemini("not json")
    with pytest.raises(ExtractionError, match="malformed JSON"):
        candidate(client=dummy).extract(Image.new("RGB", (500, 500), "white"))


@pytest.mark.parametrize("candidate", [OpenAIBenchmarkExtractor, GeminiBenchmarkExtractor])
def test_candidate_network_failure_does_not_become_fabricated_ocr(candidate):
    class FailingClient:
        responses = None
        models = None
        def __init__(self):
            self.responses = self
            self.models = self
        def create(self, **kwargs):
            raise TimeoutError("private connection failure")
        def generate_content(self, **kwargs):
            raise TimeoutError("private connection failure")

    with pytest.raises(ExtractionError, match="request failed"):
        candidate(client=FailingClient()).extract(Image.new("RGB", (400, 400), "white"))


def test_comparison_provider_selection_does_not_call_paid_apis(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", None)
    monkeypatch.setattr(settings, "gemini_api_key", None)
    with pytest.raises(ExtractionError, match="OPENAI_API_KEY is not set"):
        make_benchmark_extractor("openai")
    with pytest.raises(ExtractionError, match="GEMINI_API_KEY is not set"):
        make_benchmark_extractor("gemini")
    with pytest.raises(ValueError, match="Unknown benchmark provider"):
        make_benchmark_extractor("unsupported")
