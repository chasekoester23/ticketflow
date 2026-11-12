"""Container healthcheck: GET /health/ready on this container, 0 if ready.

The API refuses any Host it doesn't trust, and production refuses local names in
TRUSTED_HOSTS, so this asks with the first trusted host as the Host header.
Standard library only.
"""
import json
import os
import sys
import urllib.request


def main() -> int:
    try:
        hosts = json.loads(os.environ.get("TRUSTED_HOSTS") or "[]")
    except ValueError:
        hosts = []
    host = hosts[0] if hosts and isinstance(hosts[0], str) else "localhost"
    request = urllib.request.Request(f"http://127.0.0.1:{os.environ.get('PORT', '8080')}/health/ready", headers={"Host": host})
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            return 0 if response.status == 200 else 1
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
