"""The single way to read and write stored documents.

New rows store a path relative to STORAGE_DIR, such as ``uploads/12/abc.pdf``. Older
rows hold a full path from wherever the app ran at the time, so a database restored
somewhere new would otherwise lose every scan. A full path is first looked for under
the current STORAGE_DIR, from its ``uploads``, ``crops`` or ``invoices`` folder on.
Only if it isn't there is the old location tried.

The methods on LocalStorage are the whole interface. ``object_store.ObjectStorage``
implements the same methods for a private S3-compatible bucket (Cloudflare R2 in
production), selected by STORAGE_BACKEND=s3. Keys are the same in both.
"""
from __future__ import annotations

import io
import os
import shutil
import uuid
from pathlib import Path, PurePosixPath

from fastapi.responses import FileResponse, Response
from PIL import Image

from ..config import settings

# Every document lives in one of these. Backups, restores and the lookup of older
# full paths all depend on it.
SECTIONS = ("uploads", "crops", "invoices")
_MISSING = "The stored file is missing."


class DocumentMissing(OSError):
    """Never carries a path, so its message is safe to show or to store on a record."""

    def __init__(self) -> None:
        super().__init__(_MISSING)


def key_for(reference: str | None) -> str | None:
    """The location-independent key of a stored reference, or None if it couldn't be
    one TicketFlow wrote. The last matching section wins, because an older install's
    own path can include one of these names."""
    if not reference:
        return None
    parts = PurePosixPath(reference).parts
    if not os.path.isabs(reference):
        return "/".join(parts) if _is_key(parts) else None
    for start in range(len(parts) - 1, -1, -1):
        if parts[start] in SECTIONS and _is_key(parts[start:]):
            return "/".join(parts[start:])
    return None


def _is_key(parts: tuple[str, ...]) -> bool:
    return len(parts) >= 2 and parts[0] in SECTIONS and ".." not in parts and "." not in parts


class LocalStorage:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _inside(self, key: str) -> Path | None:
        """Where a key would be, unless a symlink or anything else leads out of storage."""
        path = self.root / key
        try:
            path.resolve().relative_to(self.root.resolve())
        except (ValueError, OSError):
            return None
        return path

    def locate(self, reference: str | None, *, inside_only: bool = False) -> Path | None:
        """Where this document is on disk now, or None. ``inside_only`` ignores an older
        location, so checking a restored copy can't be fooled by the original still
        being on this machine."""
        key = key_for(reference)
        if key is None:
            return None
        path = self._inside(key)
        if path is not None and path.is_file():
            return path
        if not inside_only and os.path.isabs(reference):
            original = Path(reference)
            if original.is_file():
                return original
        return None

    def exists(self, reference: str | None) -> bool:
        return self.locate(reference) is not None

    def read(self, reference: str | None) -> bytes:
        path = self.locate(reference)
        if path is None:
            raise DocumentMissing()
        return path.read_bytes()

    def save(self, key: str, data: bytes, *, replace: bool = False) -> str:
        """Store privately and return the reference to keep on the row. All or nothing:
        a half-written crop would otherwise be found by a retry and kept for good."""
        path = self._writable(key)
        temporary = path.with_name(f".{uuid.uuid4().hex}.partial")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if replace:
                os.replace(temporary, path)
            else:
                os.link(temporary, path)  # Unlike a rename, refuses to replace an existing file.
        finally:
            temporary.unlink(missing_ok=True)
        return key

    def save_png(self, key: str, image: Image.Image, *, replace: bool = False) -> str:
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        return self.save(key, stream.getvalue(), replace=replace)

    def delete(self, reference: str | None) -> None:
        key = key_for(reference)
        path = self._inside(key) if key else None
        if path is not None:
            path.unlink(missing_ok=True)

    def delete_prefix(self, key: str) -> None:
        """Remove one folder of documents, such as a rejected upload's."""
        parts = PurePosixPath(key).parts
        path = self._inside(key) if _is_key(parts) and not os.path.isabs(key) else None
        if path is None:
            raise ValueError("Not a folder of stored documents.")
        shutil.rmtree(path, ignore_errors=True)

    def serve(self, reference: str | None, media_type: str) -> Response | None:
        """A response holding this document, or None if it's missing. Callers have already
        checked the signed-in company owns the row this reference came from."""
        path = self.locate(reference)
        return None if path is None else FileResponse(path, media_type=media_type)

    def _writable(self, key: str) -> Path:
        parts = PurePosixPath(key).parts
        if os.path.isabs(key) or not _is_key(parts):
            raise ValueError("Not a stored document's key.")
        path = self._inside(key)
        if path is None:
            raise ValueError("Not a stored document's key.")
        # Every folder created is private, not only the last one. Another request may
        # create the same folder at the same moment, which is fine.
        missing = [parent for parent in (path.parent, *path.parent.parents) if not parent.exists()]
        for folder in reversed(missing):
            folder.mkdir(mode=0o700, exist_ok=True)
        return path


def current() -> LocalStorage:
    """The store for this installation. Read each time, so it follows STORAGE_DIR and
    STORAGE_BACKEND. The object store has the same methods (see object_store.py)."""
    if settings.storage_backend == "s3":
        from . import object_store
        return object_store.from_settings(settings)
    return LocalStorage(settings.storage_dir)


def reference_for(reference: str) -> str:
    """What a new row should hold for something an older row refers to."""
    return key_for(reference) or reference


def rotated_key(reference: str, degrees: int) -> str:
    """Rotation is a derived file beside the original crop, which is always kept."""
    if not degrees:
        return reference
    path = PurePosixPath(reference_for(reference))
    return str(path.with_name(f"{path.stem}_r{degrees}{path.suffix}"))
