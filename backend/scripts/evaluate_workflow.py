"""Score finished pilot batches against ground truth a person wrote from the paper.

Usage from backend/ (on a COPY of the database, never the one the app is using):
  cp ticketflow.db evaluation_data/pilot-copy.db
  python scripts/evaluate_workflow.py --database evaluation_data/pilot-copy.db \
      --organization 1 --batch 12 --batch 13 --truth evaluation_data/truth.json \
      --out evaluation_data/workflow-report.json

The database is opened read-only. No model is called and nothing is written to the
database. The report holds counts and verdicts, not ticket contents. See
EVALUATION.md, "Whole-workflow evaluation", for the ground-truth format.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.services.evaluation import DatasetError  # noqa: E402
from app.services.workflow_evaluation import evaluate_workflow, load_truth  # noqa: E402


def read_only_engine(database: str):
    if "://" not in database:
        path = Path(database).resolve()
        if not path.is_file():
            raise DatasetError(f"No database file at {path}")
        return create_engine(f"sqlite:///file:{path}?mode=ro&uri=true")
    engine = create_engine(database)
    if engine.dialect.name == "postgresql":
        @event.listens_for(engine, "connect")
        def _read_only(connection, _record):
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
    else:
        raise DatasetError("Give a SQLite file path or a postgresql:// URL.")
    return engine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database", required=True, help="SQLite file (a copy) or postgresql:// URL")
    parser.add_argument("--organization", type=int, required=True)
    parser.add_argument("--batch", type=int, action="append", required=True)
    parser.add_argument("--truth", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        truth = load_truth(args.truth)
        engine = read_only_engine(args.database)
        with Session(engine) as db:
            report = evaluate_workflow(db, args.organization, args.batch, truth)
            db.rollback()
    except DatasetError as exc:
        print(f"Not evaluated: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(report, indent=2)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
