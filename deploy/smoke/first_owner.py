"""TEST ONLY: first-run setup, from inside the API container, where it's allowed.

    docker compose exec -T -e SMOKE_OWNER_PASSWORD api python - < deploy/smoke/first_owner.py

First-run setup only works from the API's own machine. The smoke test separately
checks that the same request through the public frontend is refused. Fictional names.
Standard library only.
"""
import json
import os
import sys
import urllib.error
import urllib.request

port = os.environ.get("PORT", "8080")
body = json.dumps({"company_name": "Smoke Test Paving (fictional)", "display_name": "Smoke Owner",
                   "username": os.environ.get("SMOKE_OWNER", "smoke-owner"),
                   "password": os.environ["SMOKE_OWNER_PASSWORD"]}).encode()
request = urllib.request.Request(f"http://127.0.0.1:{port}/api/setup", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-TicketFlow-Request": "1",
                                          "Host": json.loads(os.environ.get("TRUSTED_HOSTS", '["localhost"]'))[0]})
try:
    with urllib.request.urlopen(request, timeout=30) as response:
        print("first-owner setup:", response.status)
except urllib.error.HTTPError as error:
    print("first-owner setup failed:", error.code, error.read()[:300], file=sys.stderr)
    sys.exit(1)
