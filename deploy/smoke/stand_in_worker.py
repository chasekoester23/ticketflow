"""TEST ONLY: the real worker loop with a stand-in reader in place of the vision API.

Used by the deploy smoke test (.github/workflows/deploy-validation.yml), mounted into
the backend image, which itself contains no test code. Every value it returns is
fictional. No AI provider is called, and no key is needed.
"""
from __future__ import annotations

import itertools
import os
import sys
import threading

sys.path.insert(0, os.environ.get("TICKETFLOW_BACKEND", "/app"))

from app.services.validation import ExtractedTicket  # noqa: E402
from scripts import run_worker  # noqa: E402

_FICTIONAL = [
    ("No. 57 Crushed Stone", "24.50", "Example Aggregates"),
    ("Sand", "19.75", "Sample Materials"),
    ("No. 8 Stone", "22.10", "Example Aggregates"),
    ("Fill Dirt", "18.20", "Sample Materials"),
]


class StandInReader:
    """Answers every crop with the next fictional ticket, each with its own number."""

    _numbers = itertools.count(91001)
    _lock = threading.Lock()

    def extract(self, _image):
        with self._lock:
            number = next(self._numbers)
        material, tons, supplier = _FICTIONAL[number % len(_FICTIONAL)]
        raw = {"ticket_number": str(number), "delivery_date": "2026-10-15", "material_type": material,
               "tonnage": tons, "supplier": supplier, "job_location": "Smoke Test Site"}
        return ExtractedTicket.model_validate(raw), dict(raw)


run_worker.get_extractor = StandInReader
if __name__ == "__main__":
    raise SystemExit(run_worker.main())
