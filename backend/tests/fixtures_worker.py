"""The real worker, stopped at a chosen moment so a kill lands exactly there.

Runs scripts/run_worker.py's own loop against the real database and files. All it
adds: a pause at TF_PAUSE_AT, and a stand-in reader in place of the vision API, which
logs each read to TF_READ_LOG. Started by fixtures_stack.Stack. No AI, no network.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import jobqueue, pipeline, scan_manifest  # noqa: E402
from app.services.validation import ExtractedTicket  # noqa: E402
from scripts import run_worker  # noqa: E402

POINT = os.environ.get("TF_PAUSE_AT", "")
counts = {"crops": 0, "files": 0, "writes": 0}


def arrive() -> None:
    """Say we're here, then wait to be killed."""
    Path(os.environ["TF_PAUSE_FILE"]).touch()
    while True:
        time.sleep(60)


class LoggedReader:
    """Stands in for the vision API, and leaves a record of every read."""

    def extract(self, _image):
        with open(os.environ["TF_READ_LOG"], "a") as log:
            log.write("read\n")
        raw = {"ticket_number": None, "delivery_date": "2026-09-15", "material_type": "Sand", "tonnage": "20.000",
               "supplier": "Example Aggregates", "job_location": "Lot"}
        return ExtractedTicket.model_validate(raw), json.loads(json.dumps(raw))


real_batch = jobqueue.pipeline.process_batch
real_file = pipeline.process_file
real_slot = scan_manifest.record_slot
real_png = scan_manifest.write_private_png


def process_batch(*args, **kwargs):
    if POINT == "after_claim":
        arrive()
    real_batch(*args, **kwargs)
    if POINT == "before_finish":
        arrive()


def process_file(*args, **kwargs):
    counts["files"] += 1
    if POINT == "between_files" and counts["files"] == 2:
        arrive()
    return real_file(*args, **kwargs)


def record_slot(*args, **kwargs):
    counts["crops"] += 1
    if POINT == "mid_file" and counts["crops"] == 6:  # Second file, third slot.
        arrive()
    return real_slot(*args, **kwargs)


def write_private_png(path, image):
    """Dies with the file half written, as a kill during the write would leave it."""
    counts["writes"] += 1
    if POINT == "mid_crop_write" and counts["writes"] == 6:
        import io
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        data = buffer.getvalue()
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(descriptor, data[: len(data) // 2])
        os.close(descriptor)
        arrive()
    return real_png(path, image)


jobqueue.pipeline.process_batch = process_batch
pipeline.process_file = process_file
scan_manifest.record_slot = record_slot
scan_manifest.write_private_png = write_private_png
run_worker.get_extractor = LoggedReader

if __name__ == "__main__":
    raise SystemExit(run_worker.run(once=False, poll_seconds=0.25))
