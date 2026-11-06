"""The worker's logs: one JSON line per event, with ids and never document contents.

An exception's text can quote what it failed on (a pydantic error quotes its input,
which here is a ticket's values). So logs keep the error's type and the code location
it came from, and leave out its message. A log line is safe to paste into a support
request. Another library's message is left out too, for the same reason.
"""
from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import datetime, timezone
from types import TracebackType

_OURS = ("app", "ticketflow")
_FIELDS = ("job_id", "batch_id", "file_id", "attempt", "attempts", "max_attempts", "batch_status", "ms", "poll_seconds", "jobs")


def frames(tb: TracebackType | None) -> list[str]:
    """Where an error happened: file, line and function. Source lines are left out
    too, in case one holds a value, and so is the exception's message."""
    return [f"{frame.filename.rsplit('/', 1)[-1]}:{frame.lineno} {frame.name}" for frame in traceback.extract_tb(tb)]


class JsonLines(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ours = record.name.split(".", 1)[0] in _OURS
        entry = {
            "at": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname, "logger": record.name,
            "event": record.getMessage() if ours else "library_log",
        }
        for field in _FIELDS:
            if hasattr(record, field):
                entry[field] = getattr(record, field)
        if record.exc_info and record.exc_info[0] is not None:
            entry["error"] = record.exc_info[0].__name__
            entry["trace"] = frames(record.exc_info[2])
        return json.dumps(entry)


def configure() -> None:
    """JSON on stderr. Only our own loggers speak at INFO, because libraries log whole
    request URLs at that level."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonLines())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.WARNING)
    for name in _OURS:
        logging.getLogger(name).setLevel(logging.INFO)
