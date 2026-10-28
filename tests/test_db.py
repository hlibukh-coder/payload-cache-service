from pathlib import Path

import pytest

from payload_cache import db
from payload_cache.db import make_engine


def test_unsupported_databases_are_refused_before_the_first_query() -> None:
    with pytest.raises(ValueError, match="mysql"):
        make_engine("mysql://user@host/db")


def test_old_sqlite_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(db.sqlite3, "sqlite_version_info", (3, 34, 0))

    with pytest.raises(RuntimeError, match="RETURNING"):
        make_engine(f"sqlite:///{tmp_path / 'test.db'}")


def test_sqlite_engine_creates_the_missing_directory(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'nested' / 'dir' / 'test.db'}")
    engine.dispose()

    assert (tmp_path / "nested" / "dir").is_dir()
