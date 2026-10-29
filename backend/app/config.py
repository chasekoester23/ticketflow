"""Application settings. Every secret comes from environment variables (or a local .env file)."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = f"sqlite:///{BASE_DIR / 'ticketflow.db'}"
    storage_dir: Path = BASE_DIR / "storage"

    # Vision extraction. No key => extraction fails loudly (we never fake OCR).
    anthropic_api_key: str | None = None
    extraction_model: str = "claude-sonnet-5-5"

    max_upload_bytes: int = 25 * 1024 * 1024
    render_dpi: int = 200
    # Tonnage above this is flagged for review (a single tandem truck rarely exceeds ~30 t).
    max_plausible_tons: float = 60.0

    cors_origins: list[str] = ["http://localhost:3000"]
    # True for HTTPS production. False is allowed only for the localhost dev UI.
    session_secure_cookie: bool = False


settings = Settings()
