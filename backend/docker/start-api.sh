#!/bin/sh
# Starts the API. Railway sets PORT. HOST defaults to "::", which also accepts IPv4,
# so Railway's private network (IPv6) and its edge both reach it.
#
# --proxy-headers believes X-Forwarded-For/-Proto only from FORWARDED_ALLOW_IPS. The API
# must be reachable only on the private network, behind the frontend's same-origin proxy,
# which forwards just the address the platform's edge recorded. See docs/deploy/railway.md.
set -eu
exec uvicorn app.main:app \
  --host "${HOST:-::}" \
  --port "${PORT:-8080}" \
  --proxy-headers \
  --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}" \
  --no-server-header \
  --timeout-graceful-shutdown 25 \
  --workers "${WEB_CONCURRENCY:-1}"
