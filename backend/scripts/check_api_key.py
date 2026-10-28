"""Verify your Anthropic API key works: `python scripts/check_api_key.py` (run from the backend folder).

Makes one tiny real API call (a few tokens, effectively free) and prints a clear result.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402


def main() -> int:
    if not settings.anthropic_api_key:
        print("FAIL: ANTHROPIC_API_KEY is empty. Copy .env.example to .env and paste your key after the '='.")
        return 1
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=30.0, max_retries=0)
    try:
        resp = client.messages.create(
            model=settings.extraction_model, max_tokens=10,
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
        )
    except anthropic.AuthenticationError:
        print("FAIL: the key was rejected (invalid or revoked). Create a new one at console.anthropic.com.")
        return 1
    except anthropic.NotFoundError:
        print(f"FAIL: model '{settings.extraction_model}' not found. Set EXTRACTION_MODEL in .env to a model your account can use.")
        return 1
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}")
        return 1
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    print(f"OK: key works with model '{settings.extraction_model}' (reply: {text!r})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
