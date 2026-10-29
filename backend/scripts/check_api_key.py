"""Check configured TicketFlow extraction provider with one small REAL paid API call.

Run from backend/: python scripts/check_api_key.py
The script never prints the API key and does not send any ticket document.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402


def main() -> int:
    provider = settings.extraction_provider
    if provider == "gemini":
        model = settings.gemini_model
        if not settings.gemini_api_key:
            print("FAIL: GEMINI_API_KEY is missing in backend/.env. No API calls made.")
            return 1
        try:
            from google import genai
            client = genai.Client(
                api_key=settings.gemini_api_key, http_options={"timeout": 30000},
            )
            response = client.models.generate_content(
                model=model, contents="Reply with the single word: ok",
            )
            if not response.text:
                print("FAIL: Gemini model returned an empty response; check access and quotas.")
                return 1
        except Exception as exc:
            print(f"FAIL: Gemini API request failed ({type(exc).__name__}). "
                  "Check your key, enabled model, billing and quota.")
            return 1
    elif provider == "anthropic":
        model = settings.extraction_model
        if not settings.anthropic_api_key:
            print("FAIL: ANTHROPIC_API_KEY is missing in backend/.env. No API calls made.")
            return 1
        try:
            import anthropic
            client = anthropic.Anthropic(
                api_key=settings.anthropic_api_key, timeout=30.0, max_retries=0,
            )
            response = client.messages.create(
                model=model, max_tokens=10,
                messages=[{"role": "user", "content": "Reply with the single word: ok"}],
            )
            if not any(
                getattr(part, "type", None) == "text" and part.text
                for part in response.content
            ):
                print("FAIL: Anthropic model returned an empty response.")
                return 1
        except Exception as exc:
            print(f"FAIL: Anthropic API request failed ({type(exc).__name__}). "
                  "Check your key, model, billing and quota.")
            return 1
    else:
        print("FAIL: EXTRACTION_PROVIDER must be gemini or anthropic")
        return 1

    print(f"OK: {provider} key and model '{model}' responded successfully.")
    print("This does NOT establish real handwriting recognition accuracy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
