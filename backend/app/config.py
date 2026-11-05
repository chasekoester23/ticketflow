"""Application settings. Every secret comes from environment variables (or a local .env file)."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = f"sqlite:///{BASE_DIR / 'ticketflow.db'}"
    storage_dir: Path = BASE_DIR / "storage"

    # Gemini is the primary image reader. Claude remains an explicit fallback
    # option, not an automatic hidden retry or an accidental paid API call.
    # Missing keys always fail visibly; manual entry never needs a key.
    extraction_provider: str = "gemini"  # gemini | anthropic
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.1-pro-preview"
    anthropic_api_key: str | None = None
    extraction_model: str = "claude-sonnet-5-5"  # legacy Anthropic model setting
    # Optional comparison-only provider; never selected by the live worker.
    openai_api_key: str | None = None

    # How invoice PDFs become draft lines: text_layer (this machine only, no
    # network or AI) or manual (no drafting). No AI invoice reader exists yet.
    invoice_extractor: str = "text_layer"

    # Opt-in pilot measurement of review time. Off unless explicitly enabled.
    review_timing_enabled: bool = False

    max_upload_bytes: int = 25 * 1024 * 1024
    # Limit work before OCR: compressed PDFs/photos can otherwise allocate
    # enormous amounts of memory despite having a small uploaded file size.
    max_batch_files: int = 10
    max_batch_total_bytes: int = 50 * 1024 * 1024
    max_pdf_pages: int = 20
    max_rendered_page_pixels: int = 14_000_000
    max_total_pdf_pixels: int = 70_000_000
    max_image_pixels: int = 14_000_000
    render_dpi: int = 200
    # Tonnage above this is flagged for review (a single tandem truck rarely exceeds ~30 t).
    max_plausible_tons: float = 60.0

    cors_origins: list[str] = ["http://localhost:3000"]
    # Never expose this development backend through an arbitrary Host header.
    # Add the exact API domain explicitly when preparing a private deployment.
    trusted_hosts: list[str] = ["localhost", "127.0.0.1", "testserver"]
    # True for HTTPS production. False is allowed only for the localhost dev UI.
    session_secure_cookie: bool = False


settings = Settings()
