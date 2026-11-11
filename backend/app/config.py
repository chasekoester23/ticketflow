"""Application settings. Every secret comes from environment variables (or a local .env file)."""
import ipaddress
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = f"sqlite:///{BASE_DIR / 'ticketflow.db'}"
    # PostgreSQL only. SQLite has one connection per request and no such limits.
    database_pool_size: int = 10
    database_pool_overflow: int = 10
    database_lock_timeout_ms: int = 10_000
    database_statement_timeout_ms: int = 120_000
    storage_dir: Path = BASE_DIR / "storage"
    # Where scripts/backup.py writes, and where the system page looks.
    backup_dir: Path = BASE_DIR / "backups"

    # Gemini is the primary image reader. Claude remains an explicit fallback
    # option, not an automatic hidden retry or an accidental paid API call.
    # Missing keys always fail visibly; manual entry never needs a key.
    extraction_provider: str = "gemini"  # gemini | anthropic
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.1-pro-preview"
    # How the worker talks to the reader. See docs/engineering/ocr-performance.md.
    # Tickets read at the same time. Each one is a separate request to the provider.
    extraction_concurrency: int = Field(default=4, ge=1, le=16)
    # Tries per ticket for a temporary failure (rate limit, 5xx, timeout). 1 means no retry.
    extraction_max_attempts: int = Field(default=3, ge=1, le=6)
    extraction_retry_base_seconds: float = Field(default=1.0, ge=0, le=30)
    extraction_retry_max_seconds: float = Field(default=20.0, ge=0, le=120)
    extraction_timeout_seconds: float = Field(default=60.0, ge=5, le=600)
    # webp_lossless sends exactly the pixels png would, at about 40% less to upload on a
    # real scan. jpeg is far smaller but lossy: switch only after a live benchmark agrees.
    gemini_image_format: Literal["png", "webp_lossless", "jpeg"] = "webp_lossless"
    gemini_jpeg_quality: int = Field(default=92, ge=60, le=100)
    gemini_max_image_side: int = Field(default=1800, ge=512, le=4096)
    # Unset leaves the model's own default. Lower thinking is faster; measure before changing it.
    gemini_thinking_level: Literal["minimal", "low", "medium", "high"] | None = None
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
    # How many times one job may take the worker down before it's set aside for a person.
    max_job_attempts: int = Field(default=3, ge=1)
    # Tonnage above this is flagged for review (a single tandem truck rarely exceeds ~30 t).
    max_plausible_tons: float = 60.0

    cors_origins: list[str] = ["http://localhost:3000"]
    # Never expose this development backend through an arbitrary Host header.
    # Add the exact API domain explicitly when preparing a private deployment.
    trusted_hosts: list[str] = ["localhost", "127.0.0.1", "testserver"]
    # True for HTTPS production. False is allowed only for the localhost dev UI.
    session_secure_cookie: bool = False
    # "production" makes startup refuse anything unsafe for a hosted deployment.
    ticketflow_env: str = "development"
    # /docs, /redoc and /openapi.json describe every endpoint to anyone who asks. Off unless wanted.
    enable_api_docs: bool = False
    # A signed-in session ends after this long with no requests, as well as 12 hours after sign-in.
    session_idle_minutes: int = Field(default=120, ge=5, le=720)
    # Per person, per minute, in each API process. 0 turns one off. See app/rate_limit.py.
    rate_limit_uploads_per_minute: int = Field(default=30, ge=0)
    rate_limit_exports_per_minute: int = Field(default=30, ge=0)
    rate_limit_reports_per_minute: int = Field(default=120, ge=0)
    # Per network address, for the public request-access form.
    rate_limit_access_requests_per_minute: int = Field(default=5, ge=0)
    # First-run setup works only from this machine, unless this is set for that one step.
    allow_remote_setup: bool = False

    # ---- Deployment: document storage. See docs/deploy/railway.md.
    # local keeps documents under STORAGE_DIR. s3 keeps them in one private S3-compatible
    # bucket (Cloudflare R2 in production) through app/services/object_store.py.
    storage_backend: Literal["local", "s3"] = "local"
    # R2: https://<account id>.r2.cloudflarestorage.com. Empty means AWS S3 itself.
    s3_endpoint_url: str | None = None
    s3_bucket: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: SecretStr | None = None
    # R2 accepts "auto". MinIO and AWS want a real region name.
    s3_region: str = "auto"
    # Optional folder inside the bucket, such as "staging". Never stored on a row.
    s3_prefix: str = ""
    # path suits MinIO and R2; virtual suits AWS S3.
    s3_addressing_style: Literal["path", "virtual", "auto"] = "path"
    # Optional app-level AES-256-GCM envelope encryption, on top of the provider's own
    # encryption at rest: 32 random bytes, base64-encoded. Losing it loses the documents.
    storage_encryption_key: SecretStr | None = None

    # ---- Internet-facing security, backups and alerts (docs/security/, docs/operations/) ----
    # Addresses or networks (CIDR) of the reverse proxies in front of the API, as JSON:
    # ["100.64.0.0/10", "fd00::/8"]. Only a request arriving from one of these may tell
    # us the client's address (X-Forwarded-For) or that it came over HTTPS
    # (X-Forwarded-Proto). Empty trusts no one, so the connecting address is the client.
    trusted_proxy_ips: list[str] = []
    # Refuse plain HTTP (GETs are redirected to https). Always on in production.
    require_https: bool = False
    # The exact https:// origin people load the app from, when the frontend proxies /api
    # on its own origin. Mutating requests may come from here as well as CORS_ORIGINS,
    # which can then be empty: same-origin needs no CORS at all.
    app_origin: str | None = None
    # Hosts accepted for /health, /health/live and /health/ready only, such as a
    # platform's health checker ("healthcheck.railway.app"). Never for anything else.
    healthcheck_hosts: list[str] = []
    # "strict" is safe when the app and API are one site; "lax" also suits a link into the app.
    session_cookie_samesite: Literal["lax", "strict"] = "lax"
    # scripts/backup_pg.py: AES-256-GCM key, 32 bytes as base64 or hex, and a name for it
    # that goes in each manifest so the right key can be found years later. Never logged.
    backup_encryption_key: str | None = None
    backup_encryption_key_id: str = "primary"
    # A separate, S3-compatible bucket for backups only: never the documents bucket, and
    # ideally another provider or account, so one mistake or breach can't lose both.
    backup_s3_endpoint_url: str | None = None
    backup_s3_region: str = "auto"
    backup_s3_bucket: str | None = None
    backup_s3_access_key_id: str | None = None
    backup_s3_secret_access_key: str | None = None
    backup_s3_prefix: str = "ticketflow"
    # Where scripts/backup_pg.py reads documents from: the documents bucket, with a READ-ONLY
    # credential of its own. Unset means STORAGE_DIR (a local or mounted folder).
    backup_source_s3_endpoint_url: str | None = None
    backup_source_s3_region: str = "auto"
    backup_source_s3_bucket: str | None = None
    backup_source_s3_access_key_id: str | None = None
    backup_source_s3_secret_access_key: str | None = None
    backup_source_s3_prefix: str = ""
    backup_keep_daily: int = Field(default=14, ge=1, le=400)
    backup_keep_monthly: int = Field(default=12, ge=0, le=120)
    # Optional. If set, failed backups and failed health checks POST a small JSON
    # message here (a chat webhook, an uptime service's heartbeat). Nothing else is sent.
    alert_webhook_url: str | None = None


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "testserver", "0.0.0.0"}


def configuration_problems(config: Settings) -> list[str]:
    """Settings startup refuses to run with. Some are never safe. Production adds the rest
    of what a hosted deployment needs, and fails closed on anything it doesn't recognise."""
    problems = []
    if not config.trusted_hosts or any("*" in host for host in config.trusted_hosts):
        problems.append("TRUSTED_HOSTS must list explicit API hostnames, never wildcards.")
    if not config.session_secure_cookie and any(
        not origin.startswith(("http://localhost:", "http://127.0.0.1:")) for origin in config.cors_origins
    ):
        problems.append("Set SESSION_SECURE_COOKIE=true before using a non-localhost frontend.")
    if config.ticketflow_env not in ("development", "production"):
        problems.append("TICKETFLOW_ENV must be 'development' or 'production'.")
    elif config.ticketflow_env == "production":
        if not config.database_url.startswith(("postgresql://", "postgres://", "postgresql+psycopg://")):
            problems.append("Production needs PostgreSQL: set DATABASE_URL to a postgresql:// URL.")
        if not config.session_secure_cookie:
            problems.append("Production needs SESSION_SECURE_COOKIE=true, so sessions only travel over HTTPS.")
        # Empty is allowed with APP_ORIGIN: a same-origin deployment needs no CORS (checked below).
        if any(not _exact_https_origin(origin) for origin in config.cors_origins):
            problems.append("Production needs CORS_ORIGINS to list the exact https:// address of the frontend, with no wildcards or local addresses.")
        if not config.trusted_hosts or any(host.split(":")[0].strip("[]") in _LOCAL_HOSTS for host in config.trusted_hosts):
            problems.append("Production needs TRUSTED_HOSTS to list only the API's real hostnames, with no local addresses.")
        if config.enable_api_docs:
            problems.append("Production needs ENABLE_API_DOCS=false.")
        if config.app_origin is not None and not _exact_https_origin(config.app_origin):
            problems.append("Production needs APP_ORIGIN to be the exact https:// address people open, or unset.")
    problems += _proxy_problems(config)
    problems += _storage_problems(config)
    return problems


def _exact_https_origin(origin: str) -> bool:
    host = origin.split("//", 1)[-1]
    return (origin.startswith("https://") and "*" not in origin and "/" not in host and host != ""
            and host.split(":")[0].strip("[]") not in _LOCAL_HOSTS)


def _proxy_problems(config: Settings) -> list[str]:
    problems = []
    for entry in config.trusted_proxy_ips:
        try:
            network = ipaddress.ip_network(entry.strip(), strict=False)
        except ValueError:
            problems.append(f"TRUSTED_PROXY_IPS has {entry!r}, which isn't an address or network.")
            continue
        if network.prefixlen == 0:
            problems.append("TRUSTED_PROXY_IPS must name your proxies, never every address: anyone could then pick their own address.")
    if any("*" in host or not host.strip() for host in config.healthcheck_hosts):
        problems.append("HEALTHCHECK_HOSTS must list exact hostnames, never wildcards.")
    if config.ticketflow_env == "production" and not config.cors_origins and config.app_origin is None:
        problems.append("Production needs APP_ORIGIN (same-origin) or CORS_ORIGINS (a separate frontend), so requests can be checked against it.")
    return problems


def _storage_problems(config: Settings) -> list[str]:
    """Document storage: an object store that is fully configured, and private over HTTPS in production."""
    import base64
    import re

    problems = []
    production = config.ticketflow_env == "production"
    if production and config.storage_backend != "s3":
        problems.append("Production needs STORAGE_BACKEND=s3: documents can't live on an app server's disk.")
    if config.storage_backend == "s3":
        missing = [name for name, value in (("S3_BUCKET", config.s3_bucket), ("S3_ACCESS_KEY_ID", config.s3_access_key_id),
                                            ("S3_SECRET_ACCESS_KEY", config.s3_secret_access_key)) if not value]
        if production and not config.s3_endpoint_url:
            missing.insert(0, "S3_ENDPOINT_URL")
        if missing:
            problems.append("Object storage needs " + ", ".join(missing) + ".")
        endpoint = config.s3_endpoint_url or ""
        if endpoint and not re.match(r"^https?://[^/?#@\s]+/?$", endpoint):
            problems.append("S3_ENDPOINT_URL must be the bare https:// address of the store, with no path or credentials.")
        elif production and endpoint and not endpoint.startswith("https://"):
            problems.append("Production needs S3_ENDPOINT_URL to use https://.")
        prefix = config.s3_prefix.strip("/")
        if prefix and (not re.match(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$", prefix) or {".", ".."} & set(prefix.split("/"))):
            problems.append("S3_PREFIX may hold only letters, digits, '.', '_', '-' and '/' between segments.")
    if config.storage_encryption_key is not None:
        try:
            secret = config.storage_encryption_key
            valid = len(base64.b64decode(secret.get_secret_value() if hasattr(secret, "get_secret_value") else str(secret),
                                         validate=True)) == 32
        except ValueError:
            valid = False
        if not valid:
            problems.append("STORAGE_ENCRYPTION_KEY must be 32 random bytes, base64-encoded.")
        elif config.storage_backend != "s3":
            problems.append("STORAGE_ENCRYPTION_KEY applies only to STORAGE_BACKEND=s3.")
    return problems


settings = Settings()
