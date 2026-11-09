"""Small deterministic helpers shared by the synthetic generator."""
from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any

import numpy as np


def rng(*parts: Any) -> random.Random:
    """A random stream fixed by its name. Seeding with a string is stable across
    runs and platforms (it doesn't use hash()), so one seed gives one dataset."""
    return random.Random(":".join(str(p) for p in parts))


def np_rng(*parts: Any) -> np.random.Generator:
    digest = hashlib.sha256(":".join(str(p) for p in parts).encode()).digest()
    return np.random.default_rng(int.from_bytes(digest[:8], "big"))


def dumps(data: Any, compact: bool = False) -> str:
    """Canonical JSON: same data, same bytes. Compact drops the whitespace."""
    if compact:
        return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_json(path: Path, data: Any, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(data, compact), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def r2(value: float) -> float:
    """Round geometry for storage, so float noise never reaches the labels."""
    return round(float(value), 2)
