"""Shared Alembic helpers for migration tests."""
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def alembic_config(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def head_revision() -> str:
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_current_head()
