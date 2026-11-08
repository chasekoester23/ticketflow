"""Is every document the database refers to really there, and undamaged?

Usage (from backend/):
    python scripts/check_documents.py
    python scripts/check_documents.py --database sqlite:///restored/ticketflow.db --storage restored/storage --inside-only

Reads only. Hashes each file against the hash recorded for it, which can take a while
on a large installation. Results give table names and row ids, never paths or contents.
Exit code 0 means every document is sound, 1 that some are missing or damaged.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import normalized_url  # noqa: E402
from app.services import document_audit  # noqa: E402
from app.services.storage import LocalStorage  # noqa: E402


def check_database(url: str, storage: Path, *, inside_only: bool = False) -> document_audit.Result:
    engine = create_engine(normalized_url(url), poolclass=NullPool, hide_parameters=True)
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)

            def columns_of(table: str) -> set[str]:
                return {column["name"] for column in inspector.get_columns(table)} if inspector.has_table(table) else set()

            def execute(sql: str):
                return connection.execute(text(sql)).fetchall()

            return document_audit.check(execute, columns_of, LocalStorage(storage), inside_only=inside_only)
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database", default=settings.database_url, help="Defaults to this installation's database")
    parser.add_argument("--storage", type=Path, default=settings.storage_dir, help="Defaults to this installation's STORAGE_DIR")
    parser.add_argument("--inside-only", action="store_true", help="Count a document outside the storage folder as missing")
    parser.add_argument("--json", action="store_true", help="Print the full result as JSON")
    args = parser.parse_args(argv)
    result = check_database(args.database, args.storage, inside_only=args.inside_only)
    if args.json:
        print(json.dumps(result.out(), indent=2))
    else:
        print(f"Checked {result.checked} reference(s), {result.hash_checked} of them against a recorded hash.")
        for title, count, rows in (("Missing", result.missing_count, result.missing), ("Damaged (hash differs)", result.mismatched_count, result.mismatched),
                                   ("Found only outside the storage folder", result.outside_storage_count, result.outside_storage)):
            if count:
                shown = ", ".join(f"{row['table']} {row['id']}" for row in rows)
                print(f"{title}: {count}. First few: {shown}")
        print("Every document is sound." if result.ok else "Some documents need attention. See 'Missing documents' in docs/engineering/operations.md.")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
