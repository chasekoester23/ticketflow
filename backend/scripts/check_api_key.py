"""Check configured TicketFlow extraction provider with one small REAL paid API call.

Run from backend/: python scripts/check_api_key.py
List the Gemini models your key can use (no generation, no quota used):
    python scripts/check_api_key.py --list-models
The script never prints the API key and does not send any ticket document.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.services.provider_diagnostics import explain_failure, model_name_problem  # noqa: E402


def list_gemini_models() -> int:
    if not settings.gemini_api_key:
        print("FAIL: GEMINI_API_KEY is missing in backend/.env.")
        return 1
    try:
        from google import genai
        client = genai.Client(api_key=settings.gemini_api_key, http_options={"timeout": 30000})
        names = sorted(
            m.name.removeprefix("models/") for m in client.models.list()
            if "generateContent" in (getattr(m, "supported_actions", None) or []) and "gemini" in m.name
        )
    except Exception as exc:
        print("FAIL: " + explain_failure(exc, model="(listing)", secret=settings.gemini_api_key))
        return 1
    print("Models this key can see (a listed model may still need billing):")
    for name in names:
        print(f"  {name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the configured extraction provider with one tiny request")
    parser.add_argument("--list-models", action="store_true", help="List Gemini models; makes no generation request")
    args = parser.parse_args(argv)
    if args.list_models:
        return list_gemini_models()
    provider = settings.extraction_provider
    if provider == "gemini":
        model = settings.gemini_model
        if not settings.gemini_api_key:
            print("FAIL: GEMINI_API_KEY is missing in backend/.env. No API calls made.")
            return 1
        problem = model_name_problem(model)
        if problem:
            print(f"FAIL: {problem} No API calls made.")
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
            print("FAIL: Gemini " + explain_failure(exc, model=model, secret=settings.gemini_api_key))
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
            print("FAIL: Anthropic " + explain_failure(exc, model=model, secret=settings.anthropic_api_key))
            return 1
    else:
        print("FAIL: EXTRACTION_PROVIDER must be gemini or anthropic")
        return 1

    print(f"OK: {provider} key and model '{model}' responded successfully.")
    print("This does NOT establish real handwriting recognition accuracy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
