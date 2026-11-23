"""End-to-end smoke test of a deployed stack, through the frontend's origin only.

    python deploy/smoke/smoke.py

Environment:
    SMOKE_WEB_URL         the frontend, e.g. http://127.0.0.1:3301 (the only address used for the app)
    SMOKE_OWNER           the owner made by first_owner.py (default smoke-owner)
    SMOKE_OWNER_PASSWORD  their password
    SMOKE_S3_ENDPOINT     the object store as this machine reaches it, e.g. http://127.0.0.1:9301
    SMOKE_S3_BUCKET, SMOKE_S3_ACCESS_KEY_ID, SMOKE_S3_SECRET_ACCESS_KEY, SMOKE_S3_PREFIX (optional)

Signs in, creates a project, uploads fictional scans, waits for the (stand-in) reader,
approves a ticket, exports the CSV, fetches a crop and a page image, then checks the
documents landed in the bucket and that none can be read without credentials.
Exits non-zero on the first failure. Fictional data only, and no AI provider is called.
"""
from __future__ import annotations

import csv
import io
import os
import sys
import time
import uuid
from pathlib import Path

import boto3
import httpx
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from tests.sample_data import TICKETS, make_scan_pdf  # noqa: E402  (fictional tickets, drawn with Pillow)

WEB = os.environ.get("SMOKE_WEB_URL", "http://127.0.0.1:3301").rstrip("/")
CSRF = {"X-TicketFlow-Request": "1", "Origin": WEB}
PNG = b"\x89PNG\r\n\x1a\n"


def step(message: str) -> None:
    print(f"== {message}", flush=True)


def expect(condition, message: str) -> None:
    if not condition:
        print(f"FAILED: {message}", file=sys.stderr, flush=True)
        raise SystemExit(1)


def ok(response: httpx.Response, status: int = 200) -> httpx.Response:
    expect(response.status_code == status,
           f"{response.request.method} {response.request.url.path} answered {response.status_code}, wanted {status}: {response.text[:300]}")
    expect(response.headers.get("x-request-id"), f"{response.request.url.path} carried no X-Request-ID")
    return response


def wait_for_site(client: httpx.Client) -> None:
    step("waiting for the frontend and, through it, the API")
    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            if client.get("/login").status_code == 200 and client.get("/api/setup/status").status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    expect(False, "the site never answered on /login and /api/setup/status")


def main() -> int:
    client = httpx.Client(base_url=WEB, timeout=60, follow_redirects=False)
    wait_for_site(client)

    step("the API is reached on the frontend's own origin, with the backend's headers passed through")
    status = ok(client.get("/api/setup/status", headers={"X-Request-ID": "smoke-request-1"}))
    expect(status.headers["x-request-id"] == "smoke-request-1", "X-Request-ID didn't round-trip through the proxy")
    expect(status.json() == {"needed": False}, f"first-owner setup hasn't run: {status.text}")
    expect("access-control-allow-origin" not in status.headers, "a same-origin response shouldn't need CORS headers")
    expect(status.headers.get("x-frame-options") == "DENY" and status.headers.get("x-content-type-options") == "nosniff",
           "the backend's security headers were lost")
    page = client.get("/login")
    expect("frame-ancestors 'none'" in page.headers.get("content-security-policy", ""), "the page lost its security headers")

    step("first-run setup is refused from outside the API's machine")
    refused = client.post("/api/setup", headers=CSRF, json={"company_name": "x", "display_name": "x", "username": "intruder",
                                                            "password": "not-a-real-password-123"})
    expect(refused.status_code in (403, 409), f"remote setup answered {refused.status_code}")

    step("state changes without the request header are refused (CSRF)")
    expect(client.post("/api/auth/login", json={"username": "x", "password": "y"}).status_code == 403,
           "a POST without X-TicketFlow-Request was accepted")
    expect(client.post("/api/auth/login", headers={"X-TicketFlow-Request": "1", "Origin": "https://evil.example"},
                       json={"username": "x", "password": "y"}).status_code == 403, "a foreign Origin was accepted")

    step("signing in through the frontend origin")
    login = ok(client.post("/api/auth/login", headers=CSRF, json={"username": os.environ.get("SMOKE_OWNER", "smoke-owner"),
                                                                 "password": os.environ["SMOKE_OWNER_PASSWORD"]}))
    cookies = login.headers.get_list("set-cookie")
    expect(cookies and all("httponly" in c.lower() and "samesite=lax" in c.lower() for c in cookies),
           f"the session cookie isn't HttpOnly and SameSite=Lax: {[c.split('=')[0] for c in cookies]}")
    expect(not any("domain=" in c.lower() for c in cookies), "the session cookie should be host-only, on the frontend's domain")
    me = ok(client.get("/api/auth/me")).json()
    expect(me["role"] == "owner", f"signed in as {me['role']}")

    step("creating a project")
    code = f"SMOKE-{uuid.uuid4().hex[:6].upper()}"
    project = ok(client.post("/api/projects", headers=CSRF, json={"code": code, "name": "Smoke test project (fictional)",
                                                                   "job_location": "Smoke Test Site"}), 201).json()

    step("uploading two fictional scans")
    scans = [("files", (f"smoke-{n}.pdf", make_scan_pdf(TICKETS[n * 2:n * 2 + 2] + TICKETS[:2]), "application/pdf")) for n in (0, 1)]
    batch = ok(client.post("/api/batches", headers=CSRF, files=scans,
                           data={"layout": "grid_2x2", "intake_mode": "extract", "project_id": str(project["id"])})).json()

    step("waiting for the worker to read them (stand-in reader, no AI)")
    deadline = time.time() + 240
    while True:
        current = ok(client.get(f"/api/batches/{batch['id']}")).json()
        if current["status"] in ("ready_for_review", "failed", "needs_attention", "completed"):
            break
        expect(time.time() < deadline, f"the batch is still '{current['status']}' after 4 minutes")
        time.sleep(2)
    expect(current["status"] == "ready_for_review", f"the batch ended as {current['status']}: {current.get('error')}")
    tickets = ok(client.get(f"/api/batches/{batch['id']}/tickets")).json()
    expect(len(tickets) == 8, f"expected 8 tickets from two 2x2 scans, got {len(tickets)}")

    step("fetching a crop image and a page image through the API")
    image = ok(client.get(f"/api/tickets/{tickets[0]['id']}/image"))
    expect(image.headers["content-type"] == "image/png" and image.content.startswith(PNG), "the crop isn't a PNG")
    expect("no-store" in image.headers.get("cache-control", ""), "a document image may be cached")
    file_id = current["files"][0]["id"]
    page_image = ok(client.get(f"/api/files/{file_id}/pages/1/image"))
    expect(page_image.headers["content-type"] == "image/jpeg" and page_image.content.startswith(b"\xff\xd8"),
           "the page image isn't a JPEG")
    expect(client.get("/api/tickets/999999/image").status_code == 404, "a ticket that doesn't exist didn't 404")

    step("approving one ticket")
    ticket = tickets[0]
    if ticket.get("flags"):
        print(f"   flags before approval: {ticket['flags']}")
    approved = ok(client.post(f"/api/tickets/{ticket['id']}/verify", headers=CSRF)).json()
    expect(approved["status"] == "verified", f"the ticket is {approved['status']}")

    step("exporting the CSV")
    export = ok(client.get("/api/records/export.csv", params={"project_id": project["id"]}))
    expect(export.headers["content-type"].startswith("text/csv"), f"the export is {export.headers['content-type']}")
    expect("attachment" in export.headers.get("content-disposition", ""), "the export isn't a download")
    rows = list(csv.reader(io.StringIO(export.text)))
    expect(len(rows) >= 2 and any(ticket["ticket_number"] in row for row in rows), "the approved ticket isn't in the CSV")

    step("Retry-After passes through the proxy (exports are rate limited; the stack sets a low limit)")
    throttled = None
    for _ in range(int(os.environ.get("SMOKE_EXPORT_LIMIT", "5")) + 2):
        attempt = client.get("/api/records/export.csv", params={"project_id": project["id"]})
        if attempt.status_code == 429:
            throttled = attempt
            break
    expect(throttled is not None, "exports were never rate limited; is RATE_LIMIT_EXPORTS_PER_MINUTE set low for the smoke test?")
    expect(throttled.headers.get("retry-after", "").isdigit(), "a rate-limited response carried no Retry-After")

    step("checking the documents are in the bucket, with their hashes")
    bucket = os.environ["SMOKE_S3_BUCKET"]
    prefix = os.environ.get("SMOKE_S3_PREFIX", "").strip("/")
    prefix = f"{prefix}/" if prefix else ""
    s3 = boto3.client("s3", endpoint_url=os.environ["SMOKE_S3_ENDPOINT"], region_name="us-east-1",
                      aws_access_key_id=os.environ["SMOKE_S3_ACCESS_KEY_ID"],
                      aws_secret_access_key=os.environ["SMOKE_S3_SECRET_ACCESS_KEY"],
                      config=Config(s3={"addressing_style": "path"}))
    keys = [item["Key"] for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix) for item in page.get("Contents", [])]
    uploads = [k for k in keys if k.startswith(f"{prefix}uploads/{batch['id']}/")]
    crops = [k for k in keys if k.startswith(f"{prefix}crops/")]
    expect(len(uploads) == 2, f"expected 2 uploads in the bucket, found {len(uploads)}")
    expect(len(crops) >= 8, f"expected at least 8 crops in the bucket, found {len(crops)}")
    expect(all(k.split("/")[len(prefix.split("/")) - 1] in ("uploads", "crops", "invoices") for k in keys),
           "an object under the prefix is outside the document sections")
    head = s3.head_object(Bucket=bucket, Key=uploads[0])
    expect(len(head["Metadata"].get("tf-sha256", "")) == 64, "an upload has no recorded SHA-256")

    if os.environ.get("SMOKE_STORE_ENFORCES_AUTH", "1") == "0":
        # Only for a local run against moto's server, which accepts any request. CI uses MinIO.
        step("SKIPPED: anonymous-access checks (this object store doesn't enforce credentials)")
        return _sign_out(client, tickets)
    step("checking nothing in the bucket is readable without credentials")
    anonymous = boto3.client("s3", endpoint_url=os.environ["SMOKE_S3_ENDPOINT"], region_name="us-east-1",
                             config=Config(signature_version=UNSIGNED, s3={"addressing_style": "path"}))
    for name, call in (("read an upload", lambda: anonymous.get_object(Bucket=bucket, Key=uploads[0])),
                       ("read a crop", lambda: anonymous.get_object(Bucket=bucket, Key=crops[0])),
                       ("list the bucket", lambda: anonymous.list_objects_v2(Bucket=bucket))):
        try:
            call()
        except ClientError as error:
            code = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            expect(code in (401, 403), f"anonymous attempt to {name} answered {code}")
        else:
            expect(False, f"anyone could {name} without credentials")
    plain = httpx.get(f"{os.environ['SMOKE_S3_ENDPOINT'].rstrip('/')}/{bucket}/{uploads[0]}")
    expect(plain.status_code in (401, 403), f"a plain unauthenticated GET of an upload answered {plain.status_code}")
    try:
        s3.get_bucket_policy(Bucket=bucket)
        expect(False, "the bucket has a policy; it should have none (private)")
    except ClientError as error:
        expect(error.response["Error"]["Code"] in ("NoSuchBucketPolicy", "NoSuchBucketPolicyStatus"),
               f"unexpected answer about the bucket policy: {error.response['Error']['Code']}")

    return _sign_out(client, tickets)


def _sign_out(client: httpx.Client, tickets: list[dict]) -> int:
    step("signing out ends the session")
    ok(client.post("/api/auth/logout", headers=CSRF))
    expect(client.get("/api/auth/me").status_code == 401, "still signed in after signing out")
    expect(client.get(f"/api/tickets/{tickets[0]['id']}/image").status_code == 401, "a document opened without a session")
    print("SMOKE TEST PASSED", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
