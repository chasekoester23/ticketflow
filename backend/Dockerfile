# TicketFlow backend: one image for the API and the worker, with different start commands.
#   API:    sh docker/start-api.sh          (the default)
#   Worker: python scripts/run_worker.py
# Build from backend/:  docker build -t ticketflow-backend backend
# See docs/deploy/railway.md.

FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements-dev.txt requirements-postgres.txt constraints.txt ./
# Runtime packages only: lines marked test-only (pytest, moto) stay out of the image.
# Every version comes from constraints.txt, the same pins CI tests with.
RUN grep -v "test-only" requirements-dev.txt > requirements-runtime.txt \
 && python -m venv /opt/venv \
 && /opt/venv/bin/pip install -r requirements-runtime.txt -r requirements-postgres.txt -c constraints.txt \
 && /opt/venv/bin/python -c "import cv2, fitz, PIL, psycopg, boto3, cryptography, uvicorn, alembic"

FROM python:3.12-slim
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    STORAGE_DIR=/app/var/storage \
    BACKUP_DIR=/app/var/backups
RUN groupadd --system --gid 10001 ticketflow \
 && useradd --system --uid 10001 --gid ticketflow --home-dir /app --no-create-home --shell /usr/sbin/nologin ticketflow
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
# Code is owned by root and read-only to the app. Only var/ is writable: the worker's
# lock and heartbeat, and local documents when STORAGE_BACKEND=local (never in production).
COPY . .
RUN mkdir -p /app/var/storage /app/var/backups \
 && chown -R ticketflow:ticketflow /app/var \
 && chmod 700 /app/var/storage /app/var/backups
USER 10001:10001
EXPOSE 8080
# /health/ready answers 200 only when the database does. The worker has no HTTP
# server, so its service turns this off (docker-compose.yml, railway.worker.json).
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 CMD ["python", "docker/healthcheck.py"]
CMD ["sh", "docker/start-api.sh"]
