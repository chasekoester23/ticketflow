"""The object store against a real S3-compatible server (MinIO in CI).

Skipped unless TF_S3_LIVE_ENDPOINT is set. deploy-validation.yml sets it, with:
    TF_S3_LIVE_ENDPOINT, TF_S3_LIVE_BUCKET, TF_S3_LIVE_KEY, TF_S3_LIVE_SECRET
Only ever pointed at a throwaway bucket: it writes and deletes under a random prefix.
"""
import os
import uuid

import pytest

ENDPOINT = os.environ.get("TF_S3_LIVE_ENDPOINT")
pytestmark = pytest.mark.skipif(not ENDPOINT, reason="No live S3-compatible server configured (TF_S3_LIVE_ENDPOINT).")


@pytest.fixture
def live():
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config

    from app.config import Settings
    from app.services import object_store

    config = Settings.model_construct(**{**Settings().model_dump(), **dict(
        storage_backend="s3", s3_endpoint_url=ENDPOINT, s3_bucket=os.environ["TF_S3_LIVE_BUCKET"],
        s3_access_key_id=os.environ["TF_S3_LIVE_KEY"], s3_secret_access_key=os.environ["TF_S3_LIVE_SECRET"],
        s3_region="us-east-1", s3_addressing_style="path", storage_encryption_key=None)})
    client = object_store.client_from_settings(config)
    prefix = f"live-{uuid.uuid4().hex[:8]}"
    store = object_store.ObjectStorage(client, config.s3_bucket, prefix=prefix)
    anonymous = boto3.client("s3", endpoint_url=ENDPOINT, region_name="us-east-1",
                             config=Config(signature_version=UNSIGNED, s3={"addressing_style": "path"}))
    yield store, client, anonymous, prefix
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=config.s3_bucket, Prefix=prefix + "/"):
        for item in page.get("Contents", []):
            client.delete_object(Bucket=config.s3_bucket, Key=item["Key"])


def test_round_trip_with_integrity(live):
    store, client, _, prefix = live
    store.save("uploads/1/a.pdf", b"%PDF fictional")
    assert store.read("uploads/1/a.pdf") == b"%PDF fictional"
    head = client.head_object(Bucket=store.bucket, Key=f"{prefix}/uploads/1/a.pdf")
    assert len(head["Metadata"]["tf-sha256"]) == 64


def test_the_server_itself_refuses_an_overwrite(live):
    """If-None-Match is honoured by the server, not only by the check-before-write fallback."""
    from botocore.exceptions import ClientError
    store, client, _, prefix = live
    store.save("crops/1/a.png", b"first")
    with pytest.raises(ClientError) as caught:
        client.put_object(Bucket=store.bucket, Key=f"{prefix}/crops/1/a.png", Body=b"second", IfNoneMatch="*")
    assert caught.value.response["ResponseMetadata"]["HTTPStatusCode"] == 412
    with pytest.raises(FileExistsError):
        store.save("crops/1/a.png", b"second")
    assert store.read("crops/1/a.png") == b"first"


def test_missing_and_deleted(live):
    from app.services.storage import DocumentMissing
    store, *_ = live
    with pytest.raises(DocumentMissing):
        store.read("uploads/2/none.pdf")
    store.save("uploads/2/a.pdf", b"a")
    store.save("uploads/2/b.pdf", b"b")
    store.save("uploads/20/c.pdf", b"c")
    store.delete_prefix("uploads/2")
    assert not store.exists("uploads/2/a.pdf") and not store.exists("uploads/2/b.pdf") and store.exists("uploads/20/c.pdf")


def test_envelope_encryption_on_a_real_server(live):
    from app.services.object_store import EnvelopeCipher, ObjectStorage
    store, client, _, prefix = live
    sealed = ObjectStorage(client, store.bucket, prefix=prefix, cipher=EnvelopeCipher(os.urandom(32)))
    sealed.save("invoices/1/i.pdf", b"fictional invoice text")
    raw = client.get_object(Bucket=store.bucket, Key=f"{prefix}/invoices/1/i.pdf")["Body"].read()
    assert b"fictional" not in raw
    assert sealed.read("invoices/1/i.pdf") == b"fictional invoice text"


def test_nothing_is_readable_without_credentials(live):
    from botocore.exceptions import ClientError
    store, _, anonymous, prefix = live
    store.save("uploads/3/a.pdf", b"private")
    for call in (lambda: anonymous.get_object(Bucket=store.bucket, Key=f"{prefix}/uploads/3/a.pdf"),
                 lambda: anonymous.list_objects_v2(Bucket=store.bucket),
                 lambda: anonymous.put_object(Bucket=store.bucket, Key=f"{prefix}/uploads/3/b.pdf", Body=b"x")):
        with pytest.raises(ClientError) as caught:
            call()
        assert caught.value.response["ResponseMetadata"]["HTTPStatusCode"] == 403
