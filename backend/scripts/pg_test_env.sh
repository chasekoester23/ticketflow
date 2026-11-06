#!/usr/bin/env bash
# A throwaway PostgreSQL for running TicketFlow's tests. It lives in a temporary
# folder on its own port, never touches any other database, installs nothing, and
# `stop` removes it. It needs PostgreSQL's programs (initdb, pg_ctl, createdb) on PATH,
# or PG_BIN pointing at them. On a Mac: brew install postgresql@16, then
# PG_BIN=/opt/homebrew/opt/postgresql@16/bin
#
#   backend/scripts/pg_test_env.sh start     # prints the TEST_DATABASE_URL to use
#   backend/scripts/pg_test_env.sh stop
set -euo pipefail
PORT="${PG_TEST_PORT:-55432}"
STATE="${TMPDIR:-/tmp}/ticketflow-pg-test"
BIN="${PG_BIN:+$PG_BIN/}"

case "${1:-}" in
  start)
    command -v "${BIN}initdb" >/dev/null || { echo "PostgreSQL's programs weren't found. Set PG_BIN." >&2; exit 1; }
    if [ -d "$STATE" ]; then echo "Already exists: $STATE. Run stop first." >&2; exit 1; fi
    "${BIN}initdb" -D "$STATE" -U ticketflow --auth=trust --no-sync >/dev/null
    # Listens only on this machine, and only on the socket folder and port given here.
    "${BIN}pg_ctl" -D "$STATE" -w -l "$STATE/server.log" -o "-p $PORT -c listen_addresses=127.0.0.1 -k $STATE" start >/dev/null
    "${BIN}createdb" -h 127.0.0.1 -p "$PORT" -U ticketflow ticketflow_test
    echo "export TEST_DATABASE_URL=postgresql://ticketflow@127.0.0.1:$PORT/ticketflow_test"
    ;;
  stop)
    if [ -d "$STATE" ]; then
      "${BIN}pg_ctl" -D "$STATE" -m fast stop >/dev/null || true
      rm -rf "$STATE"
    fi
    ;;
  *)
    echo "Usage: $0 start|stop" >&2
    exit 2
    ;;
esac
