"""Which documents the database refers to, and whether they're really there.

Works on any database through two small callables, so the same rules check the live
installation, a backup's copy and a restored copy. Results hold table names and row
ids only: never a path, a file name or anything from a document.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator

from .storage import LocalStorage, key_for, rotated_key

# Every column that refers to a stored document, with the hash recorded for it.
DOCUMENT_COLUMNS = (
    ("uploaded_files", "storage_path", "sha256"),
    ("invoice_documents", "storage_path", "sha256"),
    ("scan_slots", "crop_path", "crop_sha256"),
    ("tickets", "crop_path", "crop_sha256"),
)
# A rotated crop is a derived file. The recorded hash is of the original it came from.
_ROTATED = re.compile(r"_r(90|180|270)\.[a-z]+$")
_SAMPLE = 25


@dataclass(frozen=True)
class DocumentRef:
    table: str
    row_id: int
    reference: str
    sha256: str | None  # None means there's no recorded hash to compare with.


def references(execute: Callable[[str], Iterable], columns_of: Callable[[str], set[str]]) -> Iterator[DocumentRef]:
    """Every reference, skipping tables or columns an older database doesn't have."""
    for table, column, digest in DOCUMENT_COLUMNS:
        present = columns_of(table)
        if not {"id", column} <= present:
            continue
        has_digest = digest in present
        chosen = f"id, {column}" + (f", {digest}" if has_digest else "")
        for row in execute(f"SELECT {chosen} FROM {table} ORDER BY id"):
            reference = row[1]
            if reference is None:
                continue
            recorded = row[2] if has_digest and not _ROTATED.search(reference) else None
            yield DocumentRef(table, row[0], reference, recorded)
    # A slot's current rotated image is referred to by its rotation, not by a column.
    if {"id", "crop_path", "rotation"} <= columns_of("scan_slots"):
        for row_id, crop_path, rotation in execute("SELECT id, crop_path, rotation FROM scan_slots WHERE rotation <> 0 ORDER BY id"):
            yield DocumentRef("scan_slots", row_id, rotated_key(crop_path, rotation), None)


def digest_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class Result:
    checked: int = 0
    hash_checked: int = 0
    missing: list[dict] = field(default_factory=list)
    missing_count: int = 0
    mismatched: list[dict] = field(default_factory=list)
    mismatched_count: int = 0
    outside_storage: list[dict] = field(default_factory=list)
    outside_storage_count: int = 0

    @property
    def ok(self) -> bool:
        return not (self.missing_count or self.mismatched_count)

    def out(self) -> dict:
        return {"checked": self.checked, "hash_checked": self.hash_checked, "missing_count": self.missing_count, "missing": self.missing,
                "mismatched_count": self.mismatched_count, "mismatched": self.mismatched,
                "outside_storage_count": self.outside_storage_count, "outside_storage": self.outside_storage}


def _note(rows: list[dict], ref: DocumentRef) -> None:
    if len(rows) < _SAMPLE:
        rows.append({"table": ref.table, "id": ref.row_id})


def check(execute: Callable[[str], Iterable], columns_of: Callable[[str], set[str]], store: LocalStorage, *, inside_only: bool = False) -> Result:
    """Every referenced file exists and, where a hash was recorded, matches it. Each
    distinct file is hashed once, however many rows refer to it."""
    result = Result()
    seen: dict[Path, str] = {}
    for ref in references(execute, columns_of):
        result.checked += 1
        path = store.locate(ref.reference, inside_only=True)
        if path is None and not inside_only:
            path = store.locate(ref.reference)
            if path is not None:
                result.outside_storage_count += 1
                _note(result.outside_storage, ref)
        if path is None:
            result.missing_count += 1
            _note(result.missing, ref)
            continue
        if ref.sha256:
            result.hash_checked += 1
            if path not in seen:
                seen[path] = digest_of(path)
            if seen[path] != ref.sha256:
                result.mismatched_count += 1
                _note(result.mismatched, ref)
    return result


def expected_keys(execute: Callable[[str], Iterable], columns_of: Callable[[str], set[str]]) -> tuple[dict[str, DocumentRef], list[DocumentRef]]:
    """What a backup's archive must hold: {archive name: a row referring to it}, and any
    reference that could never be in an archive because it isn't one TicketFlow wrote."""
    wanted: dict[str, DocumentRef] = {}
    unknown: list[DocumentRef] = []
    for ref in references(execute, columns_of):
        key = key_for(ref.reference)
        if key is None:
            unknown.append(ref)
        elif key not in wanted or (ref.sha256 and not wanted[key].sha256):
            wanted[key] = ref
    return wanted, unknown
