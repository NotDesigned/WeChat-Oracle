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
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def init_db(db_path: Path | None = None) -> Path:
    path = db_path or settings.db_path
    settings.ensure_dirs()
    schema_sql = files("wechat_oracle").joinpath(SCHEMA_RESOURCE).read_text(encoding="utf-8")
    with _connect(path) as conn:
        conn.executescript(schema_sql)
        _migrate(conn)
    return path


def _migrate(conn: sqlite3.Connection) -> None:
    """One-shot column adds for installs that ran an older schema. Each
    block is idempotent and only fires when the column is actually missing,
    so this is safe to re-run."""
    def _has_column(table: str, col: str) -> bool:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(r["name"] == col for r in rows)

    # last_run_id added when group_memory landed (persona_drift now has the
    # back-pointer too — see schema.sql comment)
    if not _has_column("persona_drift", "last_run_id"):
        conn.execute("ALTER TABLE persona_drift ADD COLUMN last_run_id INTEGER")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_lurk_state (
            group_id       TEXT PRIMARY KEY,
            last_msg_id    INTEGER,
            last_run_id    INTEGER,
            updated_at     REAL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_proactive_outbox (
            job_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id               TEXT NOT NULL,
            group_name             TEXT,
            kind                   TEXT NOT NULL CHECK(kind IN ('committed', 'thread')),
            status                 TEXT NOT NULL CHECK(status IN ('planned', 'pending', 'running', 'sent', 'cancelled', 'expired', 'failed')),
            continuation_token     TEXT NOT NULL,
            source_run_id          INTEGER,
            source_trigger_msg_id  INTEGER,
            source_trigger_kind    TEXT,
            source_job_id          INTEGER,
            sequence               INTEGER NOT NULL DEFAULT 1,
            max_sequence           INTEGER NOT NULL DEFAULT 1,
            intent                 TEXT NOT NULL,
            reason                 TEXT NOT NULL DEFAULT '',
            delay_seconds          INTEGER NOT NULL DEFAULT 90,
            scheduled_at           REAL NOT NULL,
            expires_at             REAL NOT NULL,
            anchor_msg_id          INTEGER,
            latest_msg_id          INTEGER,
            created_at             REAL NOT NULL,
            updated_at             REAL NOT NULL,
            result                 TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_outbox_due "
        "ON agent_proactive_outbox(status, scheduled_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_outbox_group_status "
        "ON agent_proactive_outbox(group_id, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_outbox_token "
        "ON agent_proactive_outbox(continuation_token)"
    )


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
