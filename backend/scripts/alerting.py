"""Tell a person something is wrong: POST a small JSON message to ALERT_WEBHOOK_URL.

Optional and free: nothing is sent unless that setting is set, and nothing but this
message is ever sent. It suits a chat channel's incoming webhook or an uptime service's
heartbeat. The message names what failed and when, never a secret, a path, an address
or anything from a company's records. A failure to alert is printed, never raised, so
it can't hide the failure being reported.
"""
from __future__ import annotations

import json
import socket
import sys
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlsplit

_TIMEOUT_SECONDS = 10


def payload(event: str, summary: str, *, details: dict | None = None) -> dict:
    """The whole message. ``text`` makes it readable in Slack, Mattermost and Discord-style
    webhooks; the rest is for anything that reads JSON."""
    host = socket.gethostname().split(".")[0][:40]
    return {"text": f"TicketFlow: {summary}", "event": event, "summary": summary, "source": host,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "details": details or {}}


def send(url: str | None, event: str, summary: str, *, details: dict | None = None, opener=None) -> bool:
    """True if the webhook accepted it. Never raises."""
    if not url:
        return False
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.netloc:
        print("ALERT_WEBHOOK_URL must be an http(s) URL; no alert was sent.", file=sys.stderr)
        return False
    body = json.dumps(payload(event, summary, details=details)).encode()
    request = urllib.request.Request(url, data=body, method="POST",  # noqa: S310 - scheme checked above
                                     headers={"Content-Type": "application/json", "User-Agent": "ticketflow-alert"})
    try:
        with (opener or urllib.request.urlopen)(request, timeout=_TIMEOUT_SECONDS) as response:  # nosec B310
            return 200 <= response.status < 300
    except Exception as exc:  # noqa: BLE001 - alerting must never raise
        # The URL is a secret of sorts (it lets anyone post): say only that it failed.
        print(f"The alert webhook couldn't be reached ({type(exc).__name__}).", file=sys.stderr)
        return False
