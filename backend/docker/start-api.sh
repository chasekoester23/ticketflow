#!/bin/sh
# Starts the API. Railway sets PORT. HOST defaults to "::", which also accepts IPv4,
# so Railway's private network (IPv6) and its edge both reach it.
#
# The app reads X-Forwarded-For/-Proto itself (app/proxy.py), believing them only from
# TRUSTED_PROXY_IPS, so uvicorn must leave them alone: handling them twice would let the
# second step trust what the first already rewrote. The API must be reachable only on the
# private network, behind the frontend's same-origin proxy, which forwards just the address
# the platform's edge recorded. See docs/deploy/railway.md and docs/security/internet-facing.md.
set -eu
exec uvicorn app.main:app \
  --host "${HOST:-::}" \
  --port "${PORT:-8080}" \
  --no-proxy-headers \
  --no-server-header \
  --timeout-graceful-shutdown 25 \
  --workers "${WEB_CONCURRENCY:-1}"
