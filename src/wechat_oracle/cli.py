"""Typer CLI entry point: `wechat-oracle <subcommand>`.

Three production subcommands (each is a long-running process or one-shot job):
  - `init-db`           — create schema (idempotent)
  - `ingest backfill`   — one-shot import of historical export files
  - `ingest live`       — long-running SSE subscriber → DB writer
  - `dispatcher`        — long-running DB poller → LLM → wx4py reply
  - `status`            — quick row count health check

Plus `weflow find` / `weflow sessions` for diagnosing WO_GROUPS resolution.

Adding a subcommand: also update README「快速上手」段 + 「三进程」表
(CLAUDE.md「易漂移点 F5」; the doc-sync hook will remind you).
"""
from pathlib import Path

import typer
from loguru import logger

from .config import settings
from .db import get_conn, init_db
from .ingest.backfill import import_file
from .ingest.writer import write_messages

app = typer.Typer(no_args_is_help=True, add_completion=False)
ingest_app = typer.Typer(no_args_is_help=True)
weflow_app = typer.Typer(no_args_is_help=True, help="Inspect what WeFlow's HTTP API exposes (diagnose WO_GROUPS issues, etc.)")
app.add_typer(ingest_app, name="ingest")
app.add_typer(weflow_app, name="weflow")


@app.command("init-db")
def init_db_cmd() -> None:
    """Create / migrate the SQLite database."""
    path = init_db()
    logger.info("db ready at {}", path)


@ingest_app.command("backfill")
def ingest_backfill(
    path: Path = typer.Argument(..., exists=True, readable=True, dir_okay=False),
    fmt: str = typer.Option("jsonl", "--format", "-f", help="weflow | jsonl"),
) -> None:
    """Import a historical export file into the messages table."""
    init_db()
    settings.ensure_dirs()
    msgs = import_file(path, fmt, settings.data_dir)
    with get_conn() as conn:
        attempted, inserted = write_messages(conn, msgs)
    typer.echo(f"backfill: attempted={attempted} new={inserted}")


@ingest_app.command("live")
def ingest_live() -> None:
    """Poll WeFlow's HTTP API for new messages in WO_GROUPS. Requires WeFlow running."""
    from .ingest.live import run_live
    run_live()


@app.command("dispatcher")
def dispatcher_cmd() -> None:
    """Watch DB for `@<bot> /find ...` commands; print results to stdout + log.

    Requires WO_BOT_NAME and WO_DEEPSEEK_API_KEY in .env. Runs in foreground;
    Ctrl+C to stop. Safe to run alongside `ingest live`.
    """
    from .dispatcher import run_dispatcher
    run_dispatcher()


@weflow_app.command("find")
def weflow_find(keyword: str = typer.Argument(..., help="Group name / remark / wxid fragment")) -> None:
    """Search both /api/v1/contacts and /api/v1/sessions for a group. Use this to find
    the correct wxid to put in WO_GROUPS when a name doesn't resolve."""
    from .ingest.live import _build_client
    with _build_client() as client:
        cresp = client.get("/api/v1/contacts", params={"keyword": keyword, "limit": 50})
        cresp.raise_for_status()
        contacts = cresp.json().get("contacts") or []
        groups = [c for c in contacts if "@chatroom" in (c.get("username") or "")]
        people = [c for c in contacts if "@chatroom" not in (c.get("username") or "")]

        sresp = client.get("/api/v1/sessions", params={"keyword": keyword, "limit": 50})
        sresp.raise_for_status()
        sessions = sresp.json().get("sessions") or []

        typer.echo(f"contacts (groups): {len(groups)}")
        for c in groups:
            typer.echo(
                f"  username={c.get('username')}  "
                f"nick={c.get('nickname')!r}  remark={c.get('remark')!r}  "
                f"display={c.get('displayName')!r}"
            )
        typer.echo(f"\ncontacts (people): {len(people)}")
        for c in people[:10]:
            typer.echo(
                f"  username={c.get('username')}  "
                f"nick={c.get('nickname')!r}  remark={c.get('remark')!r}"
            )
        typer.echo(f"\nsessions: {len(sessions)}")
        for s in sessions[:20]:
            typer.echo(
                f"  username={s.get('username')}  display={s.get('displayName')!r}  "
                f"type={s.get('sessionType')}"
            )


@weflow_app.command("sessions")
def weflow_sessions(
    keyword: str = typer.Option("", "--keyword", "-k", help="Filter by username/displayName substring"),
    limit: int = typer.Option(10000, "--limit", "-n"),
    only_groups: bool = typer.Option(False, "--groups-only", help="Only list @chatroom sessions"),
) -> None:
    """List sessions WeFlow knows about. Use this to find the exact name or wxid for WO_GROUPS."""
    from .ingest.live import _build_client
    with _build_client() as client:
        params: dict[str, str | int] = {"limit": limit}
        if keyword:
            params["keyword"] = keyword
        resp = client.get("/api/v1/sessions", params=params)
        resp.raise_for_status()
        sessions = resp.json().get("sessions", []) or []
        if only_groups:
            sessions = [s for s in sessions if "@chatroom" in (s.get("username") or "")]
        typer.echo(f"{len(sessions)} sessions:")
        for s in sessions:
            display = s.get("displayName") or "?"
            kind = s.get("sessionType") or "?"
            user = s.get("username") or "?"
            typer.echo(f"  {kind:8s}  {display!r:40s}  {user}")


@app.command("status")
def status() -> None:
    """Quick health check: db row counts."""
    init_db()
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        per_status = conn.execute(
            "SELECT status, COUNT(*) FROM messages GROUP BY status"
        ).fetchall()
        per_group = conn.execute(
            "SELECT group_name, COUNT(*) FROM messages GROUP BY group_name"
        ).fetchall()
    typer.echo(f"db: {settings.db_path}")
    typer.echo(f"total messages: {total}")
    typer.echo("by status: " + ", ".join(f"{r[0]}={r[1]}" for r in per_status))
    typer.echo("by group: " + ", ".join(f"{r[0]}={r[1]}" for r in per_group))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
