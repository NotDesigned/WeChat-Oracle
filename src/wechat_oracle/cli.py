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
import json
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
openclaw_app = typer.Typer(no_args_is_help=True, help="Tencent iLink Bot API experiments (verifying group_id support).")
worker_app = typer.Typer(no_args_is_help=True, help="Background workers that fill in derived data on messages rows.")
app.add_typer(ingest_app, name="ingest")
app.add_typer(weflow_app, name="weflow")
app.add_typer(openclaw_app, name="openclaw")
app.add_typer(worker_app, name="worker")


@worker_app.command("mm")
def worker_mm() -> None:
    """OCR images / ASR voice messages → write into messages.transcript.

    Long-running. Polls newest-first. Models lazy-load on first use:
    rapidocr-onnxruntime for images, faster-whisper (`small` by default; set
    WO_WHISPER_MODEL=tiny|base|medium|large-v3 to override) for voice. Both
    run locally on CPU — no data leaves the machine.
    """
    from .worker.mm import run_mm_worker
    run_mm_worker()


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


@openclaw_app.command("login")
def openclaw_login() -> None:
    """One-time QR login to Tencent iLink Bot. Saves token to data/openclaw-token.json."""
    from .openclaw import OpenclawClient, login_interactive, render_qr_to_terminal
    settings.ensure_dirs()
    token_path = settings.data_dir / "openclaw-token.json"
    if token_path.exists():
        typer.echo(f"[!] token already exists at {token_path}. Re-login will overwrite.")
        if not typer.confirm("Continue?", default=False):
            raise typer.Abort()
    with OpenclawClient() as client:
        session = login_interactive(client, on_qr=render_qr_to_terminal)
        session.to_json(token_path)
    typer.echo(f"[OK] logged in as bot_id={session.bot_id!r}, saved to {token_path}")


@openclaw_app.command("probe")
def openclaw_probe(
    minutes: int = typer.Option(5, "--minutes", "-m", help="Stop after this many minutes"),
    verbose: bool = typer.Option(True, "--verbose/--quiet", help="Print one line per long-poll round trip (heartbeat)"),
) -> None:
    """Long-poll getupdates and dump every inbound message verbatim. Use this
    to discover (a) whether group_id is populated in actual messages, and
    (b) what the group_id looks like for groups your bot is in.

    Have someone send a message in a test group while this runs.
    """
    import time as _time
    from .openclaw import OpenclawClient, OpenclawSession, extract_text_from_msg

    token_path = settings.data_dir / "openclaw-token.json"
    session = OpenclawSession.from_json(token_path)
    if not session:
        typer.echo(f"[ERR] no token at {token_path}; run `openclaw login` first")
        raise typer.Exit(1)
    deadline = _time.time() + minutes * 60
    typer.echo(f"polling /getupdates as bot_id={session.bot_id!r} for {minutes}m. Ctrl+C to stop early.")
    typer.echo("--- every long-poll round-trip prints a heartbeat; messages dumped in full ---\n")
    buf = ""
    seen = 0
    rounds = 0
    with OpenclawClient(session) as client:
        while _time.time() < deadline:
            t0 = _time.time()
            resp = client.get_updates(buf)
            dt = _time.time() - t0
            rounds += 1
            msgs = resp.get("msgs") or []
            new_buf = resp.get("get_updates_buf") or buf  # truthy-only update; mirrors bridge.mjs
            buf_changed = new_buf != buf
            buf = new_buf
            if verbose:
                ret = resp.get("ret")
                typer.echo(
                    f"[round {rounds}] dt={dt:.1f}s  ret={ret}  msgs={len(msgs)}  "
                    f"buf_changed={buf_changed}  buf_head={buf[:40]!r}  full_keys={sorted(resp.keys())}"
                )
            for m in msgs:
                seen += 1
                typer.echo(f"=== msg #{seen} ===")
                typer.echo(json.dumps(m, ensure_ascii=False, indent=2))
                typer.echo(f"  -> extracted text: {extract_text_from_msg(m)!r}")
                gid = m.get("group_id")
                fuid = m.get("from_user_id")
                tuid = m.get("to_user_id")
                ctok = m.get("context_token")
                typer.echo(
                    f"  group_id={gid!r}  from_user_id={fuid!r}  to_user_id={tuid!r}  "
                    f"has_context_token={ctok is not None}"
                )
                typer.echo("")
    typer.echo(f"\nstopped: {rounds} round-trip(s), {seen} message(s). final buf head: {buf[:60]!r}")


@openclaw_app.command("send")
def openclaw_send(
    text: str = typer.Argument(..., help="Body to send"),
    to_user: str = typer.Option(None, "--to-user", help="WeChat user id (xxx@im.wechat)"),
    group_id: str = typer.Option(None, "--group-id", help="Openclaw group id (from a probe session)"),
    context_token: str = typer.Option(None, "--context-token", help="Optional thread context"),
) -> None:
    """Send a single test message. Pass --group-id to test the unconfirmed
    group send path; pass --to-user for the confirmed-working DM path."""
    from .openclaw import OpenclawClient, OpenclawSession
    if not (to_user or group_id):
        typer.echo("[ERR] pass either --to-user or --group-id"); raise typer.Exit(1)
    session = OpenclawSession.from_json(settings.data_dir / "openclaw-token.json")
    if not session:
        typer.echo("[ERR] no token; run `openclaw login` first"); raise typer.Exit(1)
    with OpenclawClient(session) as client:
        try:
            cid, resp = client.send_text(
                to_user_id=to_user, group_id=group_id, text=text, context_token=context_token,
            )
        except Exception as e:
            typer.echo(f"[ERR] send failed: {e}")
            # httpx raises HTTPStatusError on non-2xx; print response body if any
            r = getattr(e, "response", None)
            if r is not None:
                try:
                    typer.echo(f"  status: {r.status_code}")
                    typer.echo(f"  body:   {r.text[:1000]}")
                except Exception:
                    pass
            raise typer.Exit(2)
    typer.echo(f"[OK] HTTP 2xx; client_id={cid!r}")
    typer.echo(f"  server response body:")
    typer.echo(json.dumps(resp, ensure_ascii=False, indent=2))


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
