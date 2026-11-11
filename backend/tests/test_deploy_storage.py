"""Private object storage (STORAGE_BACKEND=s3) and the deployment settings around it.

Runs against moto's in-memory S3, so no network and no account. The same driver is
exercised against a real MinIO server by .github/workflows/deploy-validation.yml.
"""
import base64
import hashlib
import io
import os

import pytest
from PIL import Image

from app.config import Settings, configuration_problems, settings
from app.services import object_store, storage
from app.services.object_store import DocumentDamaged, EnvelopeCipher, ObjectStorage, StorageUnavailable

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")

BUCKET = "tf-unit"
KEY = base64.b64encode(bytes(range(32))).decode()


@pytest.fixture
def s3():
    with moto.mock_aws():
        client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing")
        client.create_bucket(Bucket=BUCKET)
        yield client


@pytest.fixture
def store(s3):
    return ObjectStorage(s3, BUCKET, prefix="unit")


@pytest.fixture
def sealed(s3):
    return ObjectStorage(s3, BUCKET, cipher=EnvelopeCipher.from_setting(KEY))


@pytest.fixture
def use_s3(s3, monkeypatch):
    """The whole app on the bucket, through storage.current()."""
    for name, value in dict(storage_backend="s3", s3_bucket=BUCKET, s3_access_key_id="testing",
                            s3_secret_access_key="testing", s3_region="us-east-1", s3_endpoint_url=None,
                            s3_prefix="app").items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(object_store, "client_from_settings", lambda _config: s3)
    object_store._cache.clear()
    yield s3
    object_store._cache.clear()


# ------------------------------------------------------------------ the same interface

def test_the_object_store_has_exactly_the_local_interface():
    public = lambda cls: {name for name in vars(cls) if not name.startswith("_")}  # noqa: E731
    assert public(ObjectStorage) == public(storage.LocalStorage)


def test_save_read_and_the_key_stays_relative(store, s3):
    assert store.save("uploads/7/a.pdf", b"%PDF-1 fictional") == "uploads/7/a.pdf"
    assert store.read("uploads/7/a.pdf") == b"%PDF-1 fictional"
    assert store.exists("uploads/7/a.pdf") and store.locate("uploads/7/a.pdf") == "uploads/7/a.pdf"
    # The prefix lives in the bucket, never on the row.
    names = [item["Key"] for item in s3.list_objects_v2(Bucket=BUCKET)["Contents"]]
    assert names == ["unit/uploads/7/a.pdf"]
    head = s3.head_object(Bucket=BUCKET, Key="unit/uploads/7/a.pdf")
    assert head["Metadata"]["tf-sha256"] == hashlib.sha256(b"%PDF-1 fictional").hexdigest()


def test_a_legacy_absolute_reference_finds_its_object(store):
    store.save("crops/3/x.png", b"png")
    assert store.read("/Users/someone/old/backend/storage/crops/3/x.png") == b"png"


def test_missing_documents_raise_the_same_error_as_local(store):
    for reference in ("uploads/1/none.pdf", None, "", "elsewhere/1/x.pdf"):
        with pytest.raises(storage.DocumentMissing) as caught:
            store.read(reference)
        assert str(caught.value) == "The stored file is missing."
        assert not store.exists(reference)
    assert store.serve("uploads/1/none.pdf", "application/pdf") is None


@pytest.mark.parametrize("key", ["../uploads/1/x.pdf", "/uploads/1/x.pdf", "uploads/../crops/1/x.png", "uploads/1/../../x",
                                 "uploads", "backups/1/x.db", "uploads//1/x.pdf", "uploads/./1/x.pdf", "uploads\\1\\x.pdf",
                                 "uploads/1/x\x00.pdf", "", "storage/uploads/1/x.pdf"])
def test_keys_that_could_escape_are_refused(store, key):
    with pytest.raises(ValueError):
        store.save(key, b"x")


def test_never_overwrites_unless_asked(store):
    store.save("crops/1/a.png", b"first")
    with pytest.raises(FileExistsError):
        store.save("crops/1/a.png", b"second")
    assert store.read("crops/1/a.png") == b"first"
    store.save("crops/1/a.png", b"rotated", replace=True)
    assert store.read("crops/1/a.png") == b"rotated"


def test_a_store_without_conditional_writes_still_never_overwrites(store, s3, monkeypatch):
    from botocore.exceptions import ClientError
    real_put = s3.put_object

    def put(**request):
        if "IfNoneMatch" in request:
            raise ClientError({"Error": {"Code": "NotImplemented"}, "ResponseMetadata": {"HTTPStatusCode": 501}}, "PutObject")
        return real_put(**request)

    monkeypatch.setattr(s3, "put_object", put)
    store.save("crops/2/a.png", b"first")
    with pytest.raises(FileExistsError):
        store.save("crops/2/a.png", b"second")
    assert store.read("crops/2/a.png") == b"first"


def test_a_changed_object_is_reported_as_damaged_not_served(store, s3):
    store.save("uploads/4/a.pdf", b"original")
    s3.put_object(Bucket=BUCKET, Key="unit/uploads/4/a.pdf", Body=b"tampered",
                  Metadata={"tf-sha256": hashlib.sha256(b"original").hexdigest()})
    with pytest.raises(DocumentDamaged) as caught:
        store.read("uploads/4/a.pdf")
    assert "uploads" not in str(caught.value)


def test_a_write_that_lands_wrong_is_removed(store, s3, monkeypatch):
    real_head = s3.head_object
    monkeypatch.setattr(s3, "head_object", lambda **kw: {**real_head(**kw), "ContentLength": 1})
    with pytest.raises(DocumentDamaged):
        store.save("uploads/5/a.pdf", b"some bytes")
    monkeypatch.setattr(s3, "head_object", real_head)
    assert not store.exists("uploads/5/a.pdf")


def test_png_save_and_serve(store):
    store.save_png("crops/9/t.png", Image.new("RGB", (4, 4), "white"))
    response = store.serve("crops/9/t.png", "image/png")
    assert response.media_type == "image/png" and response.body.startswith(b"\x89PNG")


def test_delete_and_delete_prefix_stay_inside_their_folder(store):
    for key in ("uploads/1/a.pdf", "uploads/1/b.pdf", "uploads/10/c.pdf", "crops/1/d.png"):
        store.save(key, b"x")
    store.delete_prefix("uploads/1")
    assert not store.exists("uploads/1/a.pdf") and not store.exists("uploads/1/b.pdf")
    assert store.exists("uploads/10/c.pdf") and store.exists("crops/1/d.png")  # uploads/10 isn't uploads/1
    store.delete("crops/1/d.png")
    store.delete("crops/1/d.png")  # already gone is fine
    assert not store.exists("crops/1/d.png")
    for folder in ("uploads", "", "../uploads/10", "/uploads/10", "elsewhere/1"):
        with pytest.raises(ValueError):
            store.delete_prefix(folder)
    assert store.exists("uploads/10/c.pdf")


def test_provider_errors_never_reveal_keys_or_buckets(s3):
    store = ObjectStorage(s3, "no-such-bucket-here")
    with pytest.raises(StorageUnavailable) as caught:
        store.save("uploads/1/a.pdf", b"x")
    assert str(caught.value) == "Document storage is unavailable."
    with pytest.raises(OSError) as caught:
        store.read("uploads/1/a.pdf")
    assert "no-such-bucket" not in str(caught.value) and "uploads" not in str(caught.value)


def test_nothing_makes_a_public_or_presigned_url():
    source = open(object_store.__file__).read()
    assert "generate_presigned" not in source and "public-read" not in source and "ACL" not in source


# ------------------------------------------------------------------ envelope encryption

def test_encrypted_objects_are_unreadable_without_the_key(sealed, s3):
    sealed.save("invoices/1/i.pdf", b"%PDF fictional invoice")
    raw = s3.get_object(Bucket=BUCKET, Key="invoices/1/i.pdf")
    assert b"fictional" not in raw["Body"].read()
    assert raw["Metadata"]["tf-encryption"] == "aes256gcm-envelope-v1"
    assert sealed.read("invoices/1/i.pdf") == b"%PDF fictional invoice"
    # Without the key, it's unavailable, never served as if it were the document.
    with pytest.raises(StorageUnavailable):
        ObjectStorage(s3, BUCKET).read("invoices/1/i.pdf")
    other = ObjectStorage(s3, BUCKET, cipher=EnvelopeCipher(os.urandom(32)))
    with pytest.raises(DocumentDamaged):
        other.read("invoices/1/i.pdf")


def test_a_ciphertext_moved_to_another_key_wont_open(sealed, s3):
    sealed.save("invoices/1/a.pdf", b"company one")
    moved = s3.get_object(Bucket=BUCKET, Key="invoices/1/a.pdf")
    s3.put_object(Bucket=BUCKET, Key="invoices/2/a.pdf", Body=moved["Body"].read(), Metadata=moved["Metadata"])
    with pytest.raises(DocumentDamaged):
        sealed.read("invoices/2/a.pdf")


def test_plain_objects_written_before_encryption_still_read(s3, sealed):
    ObjectStorage(s3, BUCKET).save("uploads/3/old.pdf", b"older")
    assert sealed.read("uploads/3/old.pdf") == b"older"


# ------------------------------------------------------------------ the whole app on the bucket

def test_the_api_stores_and_serves_through_the_bucket(client, use_s3, tmp_path, monkeypatch):
    from .sample_data import make_invoice_pdf, make_scan_pdf
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "unused")
    batch = client.post("/api/batches", data={"layout": "grid_2x2"},
                        files=[("files", ("scan.pdf", make_scan_pdf(), "application/pdf"))])
    assert batch.status_code == 200, batch.text
    tickets = client.get(f"/api/batches/{batch.json()['id']}/tickets").json()
    assert len(tickets) == 4
    image = client.get(f"/api/tickets/{tickets[0]['id']}/image")
    assert image.status_code == 200 and image.headers["content-type"] == "image/png" and image.content.startswith(b"\x89PNG")
    invoice = client.post("/api/invoice-documents", files={"file": ("invoice.pdf", make_invoice_pdf(), "application/pdf")})
    assert invoice.status_code == 201, invoice.text
    original = client.get(f"/api/invoice-documents/{invoice.json()['id']}/original")
    assert original.status_code == 200 and original.content.startswith(b"%PDF")
    names = {item["Key"].split("/")[1] for item in use_s3.list_objects_v2(Bucket=BUCKET)["Contents"]}
    assert names == {"uploads", "crops", "invoices"}
    assert not (tmp_path / "unused" / "uploads").exists()


def test_the_document_check_reads_the_bucket_and_finds_damage(client, use_s3):
    """check_documents.py and System health use document_audit.check on the live store."""
    from sqlalchemy import inspect, text

    from app.routers import api as api_module
    from app.services import document_audit
    from .sample_data import make_scan_pdf
    assert client.post("/api/batches", data={"layout": "grid_2x2"},
                       files=[("files", ("scan.pdf", make_scan_pdf(), "application/pdf"))]).status_code == 200
    with api_module.SessionLocal() as db:
        inspector = inspect(db.connection())

        def columns_of(table):
            return {c["name"] for c in inspector.get_columns(table)} if inspector.has_table(table) else set()

        def run():
            return document_audit.check(lambda sql: db.execute(text(sql)).fetchall(), columns_of, storage.current())

        sound = run()
        assert sound.ok and sound.checked >= 5 and sound.hash_checked >= 5
        upload = next(item["Key"] for item in use_s3.list_objects_v2(Bucket=BUCKET)["Contents"] if "/uploads/" in item["Key"])
        head = use_s3.head_object(Bucket=BUCKET, Key=upload)
        use_s3.put_object(Bucket=BUCKET, Key=upload, Body=b"tampered", Metadata=head["Metadata"])
        assert run().mismatched_count == 1


# ------------------------------------------------------------------ settings

def _prod(**overrides) -> Settings:
    values = dict(ticketflow_env="production", database_url="postgresql://tf@db.internal/ticketflow", session_secure_cookie=True,
                  cors_origins=["https://app.example.com"], trusted_hosts=["app.example.com"], storage_backend="s3",
                  s3_endpoint_url="https://account.r2.cloudflarestorage.com", s3_bucket="tf-prod",
                  s3_access_key_id="placeholder", s3_secret_access_key="placeholder")
    values.update(overrides)
    return Settings.model_construct(**{**Settings().model_dump(), **values})


def test_a_complete_production_storage_configuration_passes():
    assert configuration_problems(_prod()) == []
    assert configuration_problems(_prod(storage_encryption_key=_secret(KEY), s3_prefix="prod/docs")) == []


def _secret(value):
    from pydantic import SecretStr
    return SecretStr(value)


@pytest.mark.parametrize("override, expected", [
    ({"storage_backend": "local"}, "STORAGE_BACKEND=s3"),
    ({"s3_bucket": None}, "S3_BUCKET"),
    ({"s3_access_key_id": ""}, "S3_ACCESS_KEY_ID"),
    ({"s3_secret_access_key": None}, "S3_SECRET_ACCESS_KEY"),
    ({"s3_endpoint_url": None}, "S3_ENDPOINT_URL"),
    ({"s3_endpoint_url": "http://minio:9000"}, "https://"),
    ({"s3_endpoint_url": "https://key:secret@r2.example"}, "S3_ENDPOINT_URL"),
    ({"s3_endpoint_url": "https://r2.example/bucket"}, "S3_ENDPOINT_URL"),
    ({"s3_prefix": "../escape"}, "S3_PREFIX"),
    ({"s3_prefix": "a b"}, "S3_PREFIX"),
    ({"storage_encryption_key": "too-short"}, "STORAGE_ENCRYPTION_KEY"),
    ({"session_secure_cookie": False}, "SESSION_SECURE_COOKIE"),
    ({"database_url": "sqlite:///x.db"}, "PostgreSQL"),
    ({"cors_origins": ["http://app.example.com"]}, "CORS_ORIGINS"),
])
def test_production_refuses_incomplete_or_unsafe_storage(override, expected):
    if "storage_encryption_key" in override:
        override = {"storage_encryption_key": _secret(override["storage_encryption_key"])}
    problems = configuration_problems(_prod(**override))
    assert any(expected in problem for problem in problems), problems


def test_development_allows_a_plain_http_minio():
    config = _prod(ticketflow_env="development", s3_endpoint_url="http://127.0.0.1:9000", session_secure_cookie=False,
                   cors_origins=["http://localhost:3000"], trusted_hosts=["localhost"])
    assert configuration_problems(config) == []


def test_local_storage_is_still_the_default():
    assert Settings().storage_backend == "local"
    assert isinstance(storage.current(), storage.LocalStorage)
