"""SQLite connection + schema bootstrap + transaction helper.

WAL mode is essential — the writer process (`ingest live`), the reader
(`dispatcher`), and any ad-hoc CLI command share the same DB file. WAL
allows concurrent readers + one writer.

`init_db()` runs `schema.sql` (idempotent thanks to `IF NOT EXISTS`); call
it from any entry point that touches the DB. `transaction()` is the only
sanctioned way to write — it manages BEGIN/COMMIT/ROLLBACK explicitly
because we set `isolation_level=None` (autocommit) on the connection.
"""
import sqlite3
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
from typing import Iterator

from .config import settings

SCHEMA_RESOURCE = "schema.sql"


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None)  # autocommit; we manage txns explicitly
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db(db_path: Path | None = None) -> Path:
    path = db_path or settings.db_path
    settings.ensure_dirs()
    schema_sql = files("wechat_oracle").joinpath(SCHEMA_RESOURCE).read_text(encoding="utf-8")
    with _connect(path) as conn:
        conn.executescript(schema_sql)
    return path


@contextmanager
def get_conn(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = db_path or settings.db_path
    conn = _connect(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
