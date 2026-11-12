"""Private document storage in an S3-compatible bucket, such as Cloudflare R2.

The same interface as ``storage.LocalStorage``, so every caller works unchanged:
``locate``, ``exists``, ``read``, ``save``, ``save_png``, ``delete``,
``delete_prefix`` and ``serve``. Selected with ``STORAGE_BACKEND=s3``.

What it guarantees:

- **Keys stay relative and validated.** A row holds ``uploads/12/abc.pdf`` either way.
  Nothing outside ``uploads``, ``crops`` and ``invoices`` can be read or written, and
  ``..``, absolute paths, a leading slash, backslashes and control characters are refused.
  The optional ``S3_PREFIX`` is added here and never stored on a row.
- **Never overwrites unless asked.** A write is conditional (``If-None-Match: *``). A
  store that doesn't support that gets a check before the write instead.
- **Integrity.** The SHA-256 of every document is stored with it, checked against what
  the bucket reports straight after the write, and checked again on every read.
- **No public URLs.** Nothing here makes a pre-signed or public link. Documents are read
  by the API, after its own authorisation, and streamed to the browser.
- **Optional envelope encryption** with AES-256-GCM (``STORAGE_ENCRYPTION_KEY``), on top of
  the provider's own encryption at rest. Each object gets its own data key, wrapped by the
  configured key, and both are bound to the object's key so a ciphertext can't be swapped
  for another document's.

Errors never carry a key, a bucket name or a provider message, because they can end up
on a record or in a response.
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import re
from pathlib import PurePosixPath

from fastapi.responses import Response
from PIL import Image

from .storage import SECTIONS, DocumentMissing, _is_key, key_for

_DAMAGED = "The stored file is damaged."
_UNAVAILABLE = "Document storage is unavailable."
_SHA_FIELD = "tf-sha256"
_ENCRYPTION_FIELD = "tf-encryption"
_KEY_ID_FIELD = "tf-key-id"
_SCHEME = "aes256gcm-envelope-v1"
_PREFIX = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")
_UNSAFE = re.compile(r"[\\\x00-\x1f\x7f]")


class DocumentDamaged(OSError):
    """The bytes don't match what was stored. Never carries a key."""

    def __init__(self) -> None:
        super().__init__(_DAMAGED)


class StorageUnavailable(OSError):
    """The bucket couldn't be reached or refused the request. Never carries a key."""

    def __init__(self) -> None:
        super().__init__(_UNAVAILABLE)


def valid_prefix(prefix: str) -> bool:
    """An empty prefix, or plain path segments with no leading or trailing slash."""
    return prefix == "" or (_PREFIX.match(prefix) is not None and ".." not in prefix.split("/")
                            and "." not in prefix.split("/"))


def _checked_key(key: str) -> str:
    """A key TicketFlow may write: relative, in a known section, nothing that escapes."""
    if not isinstance(key, str) or not key or key.startswith("/") or os.path.isabs(key) or _UNSAFE.search(key):
        raise ValueError("Not a stored document's key.")
    parts = PurePosixPath(key).parts
    if not _is_key(parts) or "/".join(parts) != key or any(part == "" for part in key.split("/")):
        raise ValueError("Not a stored document's key.")
    return key


class EnvelopeCipher:
    """AES-256-GCM with a fresh data key per object. Layout of what's stored:
    12-byte nonce + wrapped data key (32 + 16 tag), then 12-byte nonce + ciphertext + tag."""

    def __init__(self, key: bytes):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        if len(key) != 32:
            raise ValueError("STORAGE_ENCRYPTION_KEY must be 32 bytes, base64-encoded.")
        self._aead = AESGCM
        self._kek = AESGCM(key)
        self.key_id = hashlib.sha256(b"ticketflow-kek" + key).hexdigest()[:16]

    @classmethod
    def from_setting(cls, value: str) -> "EnvelopeCipher":
        try:
            key = base64.b64decode(value, validate=True)
        except (ValueError, TypeError):
            raise ValueError("STORAGE_ENCRYPTION_KEY must be 32 bytes, base64-encoded.") from None
        return cls(key)

    def seal(self, name: str, plaintext: bytes) -> bytes:
        data_key = self._aead.generate_key(bit_length=256)
        bound = name.encode()
        wrap_nonce, data_nonce = os.urandom(12), os.urandom(12)
        wrapped = self._kek.encrypt(wrap_nonce, data_key, b"key:" + bound)
        return wrap_nonce + wrapped + data_nonce + self._aead(data_key).encrypt(data_nonce, plaintext, b"doc:" + bound)

    def open(self, name: str, sealed: bytes) -> bytes:
        from cryptography.exceptions import InvalidTag
        bound = name.encode()
        if len(sealed) < 12 + 48 + 12 + 16:
            raise DocumentDamaged()
        try:
            data_key = self._kek.decrypt(sealed[:12], sealed[12:60], b"key:" + bound)
            return self._aead(data_key).decrypt(sealed[60:72], sealed[72:], b"doc:" + bound)
        except InvalidTag:
            raise DocumentDamaged() from None


class ObjectStorage:
    """Documents in one private bucket. ``client`` is a boto3 S3 client."""

    def __init__(self, client, bucket: str, *, prefix: str = "", cipher: EnvelopeCipher | None = None):
        if not bucket:
            raise ValueError("S3_BUCKET is required for object storage.")
        prefix = prefix.strip("/")
        if not valid_prefix(prefix):
            raise ValueError("S3_PREFIX may hold only letters, digits, '.', '_', '-' and '/' between segments.")
        self.client = client
        self.bucket = bucket
        self.prefix = f"{prefix}/" if prefix else ""
        self.cipher = cipher

    # ------------------------------------------------------------------ helpers

    def _name(self, key: str) -> str:
        return self.prefix + key

    def _errors(self):
        from botocore.exceptions import BotoCoreError, ClientError
        return ClientError, BotoCoreError

    @staticmethod
    def _code(error) -> str:
        response = getattr(error, "response", None) or {}
        return str(response.get("Error", {}).get("Code", ""))

    @staticmethod
    def _status(error) -> int:
        response = getattr(error, "response", None) or {}
        return int(response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0)

    def _missing(self, error) -> bool:
        return self._code(error) in ("NoSuchKey", "404", "NotFound") or self._status(error) == 404

    def _head(self, key: str) -> dict | None:
        client_error, core_error = self._errors()
        try:
            return self.client.head_object(Bucket=self.bucket, Key=self._name(key))
        except client_error as error:
            if self._missing(error):
                return None
            raise StorageUnavailable() from None
        except core_error:
            raise StorageUnavailable() from None

    # ------------------------------------------------------------------ the interface

    def locate(self, reference: str | None, *, inside_only: bool = False) -> str | None:
        """The object's key if it's in the bucket, or None. There is no older location
        outside the bucket to fall back to, so ``inside_only`` changes nothing."""
        key = key_for(reference)
        if key is None:
            return None
        return key if self._head(key) is not None else None

    def exists(self, reference: str | None) -> bool:
        return self.locate(reference) is not None

    def read(self, reference: str | None) -> bytes:
        key = key_for(reference)
        if key is None:
            raise DocumentMissing()
        client_error, core_error = self._errors()
        try:
            answer = self.client.get_object(Bucket=self.bucket, Key=self._name(key))
            body = answer["Body"].read()
        except client_error as error:
            if self._missing(error):
                raise DocumentMissing() from None
            raise StorageUnavailable() from None
        except core_error:
            raise StorageUnavailable() from None
        metadata = {name.lower(): value for name, value in (answer.get("Metadata") or {}).items()}
        if metadata.get(_ENCRYPTION_FIELD):
            if metadata[_ENCRYPTION_FIELD] != _SCHEME or self.cipher is None:
                # Encrypted with a scheme or key this process doesn't have. Not a missing file.
                raise StorageUnavailable()
            body = self.cipher.open(self._name(key), body)
        recorded = metadata.get(_SHA_FIELD)
        if recorded and hashlib.sha256(body).hexdigest() != recorded:
            raise DocumentDamaged()
        return body

    def save(self, key: str, data: bytes, *, replace: bool = False) -> str:
        """Store privately and return the reference to keep on the row. Refuses to
        replace an existing object unless asked, and checks what landed."""
        key = _checked_key(key)
        digest = hashlib.sha256(data).hexdigest()
        name = self._name(key)
        metadata = {_SHA_FIELD: digest}
        body = data
        if self.cipher is not None:
            body = self.cipher.seal(name, data)
            metadata.update({_ENCRYPTION_FIELD: _SCHEME, _KEY_ID_FIELD: self.cipher.key_id})
        request = {"Bucket": self.bucket, "Key": name, "Body": body, "Metadata": metadata,
                   "ContentType": "application/octet-stream",
                   # The store refuses bytes that arrive changed on the way.
                   "ContentMD5": base64.b64encode(hashlib.md5(body, usedforsecurity=False).digest()).decode()}  # transport check only
        client_error, core_error = self._errors()
        try:
            if replace:
                self.client.put_object(**request)
            else:
                self._put_new(request, key)
        except FileExistsError:
            raise
        except client_error:
            raise StorageUnavailable() from None
        except core_error:
            raise StorageUnavailable() from None
        landed = self._head(key)
        if landed is None or int(landed.get("ContentLength", -1)) != len(body) or \
                {k.lower(): v for k, v in (landed.get("Metadata") or {}).items()}.get(_SHA_FIELD) != digest:
            # Never leave a half-right object for a retry to find and keep.
            try:
                self.client.delete_object(Bucket=self.bucket, Key=name)
            except (client_error, core_error):
                pass
            raise DocumentDamaged()
        return key

    def _put_new(self, request: dict, key: str) -> None:
        client_error, _ = self._errors()
        try:
            self.client.put_object(**request, IfNoneMatch="*")
            return
        except client_error as error:
            if self._code(error) in ("PreconditionFailed", "ConditionalRequestConflict") or self._status(error) in (409, 412):
                raise FileExistsError("A stored document already has this key.") from None
            if self._code(error) not in ("NotImplemented", "InvalidArgument", "InvalidRequest") and self._status(error) != 501:
                raise
        # A store without conditional writes: check first. Keys hold a random part, so
        # the window between the check and the write is never contended in practice.
        if self._head(key) is not None:
            raise FileExistsError("A stored document already has this key.")
        self.client.put_object(**request)

    def save_png(self, key: str, image: Image.Image, *, replace: bool = False) -> str:
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        return self.save(key, stream.getvalue(), replace=replace)

    def delete(self, reference: str | None) -> None:
        key = key_for(reference)
        if key is None:
            return
        client_error, core_error = self._errors()
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self._name(_checked_key(key)))
        except (client_error, core_error):
            raise StorageUnavailable() from None

    def delete_prefix(self, key: str) -> None:
        """Remove one folder of documents, such as a rejected upload's. Never a whole
        section: ``uploads`` alone is refused, ``uploads/12`` is allowed."""
        try:
            folder = _checked_key(key.rstrip("/"))
        except ValueError:
            raise ValueError("Not a folder of stored documents.") from None
        client_error, core_error = self._errors()
        try:
            pages = self.client.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=self._name(folder) + "/")
            for page in pages:
                names = [{"Key": item["Key"]} for item in page.get("Contents", [])]
                if names:
                    self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": names, "Quiet": True})
        except (client_error, core_error):
            raise StorageUnavailable() from None

    def serve(self, reference: str | None, media_type: str) -> Response | None:
        """A response holding this document, or None if it's missing. Callers have already
        checked the signed-in company owns the row this reference came from."""
        try:
            data = self.read(reference)
        except DocumentMissing:
            return None
        return Response(content=data, media_type=media_type)


def _reveal(value) -> str | None:
    """A secret setting's value, whether it's a SecretStr or a plain string."""
    if value is None:
        return None
    return value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)


def client_from_settings(config):
    """A boto3 S3 client for the configured bucket. Imported lazily, so a local install
    never needs boto3."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=config.s3_endpoint_url or None,
        region_name=config.s3_region or "auto",
        aws_access_key_id=config.s3_access_key_id,
        aws_secret_access_key=_reveal(config.s3_secret_access_key),
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": config.s3_addressing_style},
            retries={"max_attempts": 4, "mode": "standard"},
            connect_timeout=5, read_timeout=60,
            # Newer boto3 adds CRC checksums some S3-compatible stores reject. Integrity
            # is checked here instead: Content-MD5 on the way in, SHA-256 on every read.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


_cache: dict[tuple, ObjectStorage] = {}


def from_settings(config) -> ObjectStorage:
    """One client per distinct configuration, reused across requests (boto3 clients are
    thread-safe and slow to create)."""
    identity = (config.s3_endpoint_url, config.s3_bucket, config.s3_access_key_id, _reveal(config.s3_secret_access_key),
                config.s3_region, config.s3_prefix, config.s3_addressing_style, _reveal(config.storage_encryption_key))
    store = _cache.get(identity)
    if store is None:
        cipher = EnvelopeCipher.from_setting(identity[-1]) if identity[-1] else None
        store = ObjectStorage(client_from_settings(config), config.s3_bucket or "", prefix=config.s3_prefix, cipher=cipher)
        _cache.clear()
        _cache[identity] = store
    return store


__all__ = ["ObjectStorage", "EnvelopeCipher", "DocumentDamaged", "StorageUnavailable", "SECTIONS",
           "from_settings", "client_from_settings", "valid_prefix"]
