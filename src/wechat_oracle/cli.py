"""Typer CLI entry point: `wechat-oracle <subcommand>`.

Production entry points include long-running processes, one-shot imports, and diagnostics:
  - `init-db`           - create schema (idempotent)
  - `ingest backfill`   - one-shot import of historical export files
  - `ingest live`       - long-running SSE subscriber -> DB writer
  - `dispatcher`        - long-running DB poller -> LLM -> wx4py reply
  - `status`            - quick row count health check

Plus `weflow find` / `weflow sessions` for diagnosing WO_GROUPS resolution.

Adding a subcommand: also update README quickstart + process table
(CLAUDE.md F5; the doc-sync hook will remind you).
"""
from pathlib import Path

import typer
from loguru import logger

from .config import settings
from .db import get_conn, init_db, transaction
from .ingest.backfill import import_file
from .ingest.writer import write_messages

app = typer.Typer(no_args_is_help=True, add_completion=False)
ingest_app = typer.Typer(no_args_is_help=True)
weflow_app = typer.Typer(no_args_is_help=True, help="Inspect what WeFlow's HTTP API exposes (diagnose WO_GROUPS issues, etc.)")
worker_app = typer.Typer(no_args_is_help=True, help="Background workers that fill in derived data on messages rows.")
verify_app = typer.Typer(no_args_is_help=True, help="Health checks for the dispatch pipeline.")
agent_app = typer.Typer(no_args_is_help=True, help="Inspect & manage agent memory (persona_drift / group_memory / run logs).")
openclaw_app = typer.Typer(no_args_is_help=True, help="OpenClaw runtime backend (the recommended agent path; uses subscription instead of per-token API).")
app.add_typer(ingest_app, name="ingest")
app.add_typer(weflow_app, name="weflow")
app.add_typer(worker_app, name="worker")
app.add_typer(verify_app, name="verify")
app.add_typer(agent_app, name="agent")
app.add_typer(openclaw_app, name="openclaw")


@verify_app.command("roundtrip")
def verify_roundtrip() -> None:
    """Check whether WeFlow SSE echoes the bot's own replies back into the
    messages table. Required for reply-to-bot triggers and bot_wxid
    auto-discovery.

    Run this AFTER you've @ed the bot a few times in a watched group and
    the bot has replied. Reads `messages` looking for rows where
    `sender_display == WO_BOT_NAME` (i.e. the bot's own messages).
    """
    if not settings.bot_name:
        typer.echo("WARNING:  WO_BOT_NAME is empty - set it to the bot's group nickname first.")
        raise typer.Exit(1)
    init_db()
    with get_conn() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE sender_display = ?",
            (settings.bot_name,),
        ).fetchone()["n"]
        wxid_row = conn.execute(
            "SELECT sender_wxid FROM messages "
            "WHERE sender_display = ? AND sender_wxid IS NOT NULL AND sender_wxid != '' "
            "ORDER BY t DESC LIMIT 1",
            (settings.bot_name,),
        ).fetchone()
        recent = conn.execute(
            "SELECT msg_id, group_name, t, content_text, sender_wxid FROM messages "
            "WHERE sender_display = ? ORDER BY t DESC LIMIT 5",
            (settings.bot_name,),
        ).fetchall()

    typer.echo(f"bot_name = {settings.bot_name!r}")
    typer.echo(f"messages where sender_display matches: {total}")
    if total == 0:
        typer.echo("")
        typer.echo("ERROR: No bot messages echoed back.")
        typer.echo("   Either the bot hasn't replied yet, OR WeFlow SSE doesn't")
        typer.echo("   roundtrip self-sent messages. Reply-to-bot trigger will be")
        typer.echo("   permanently disabled in this case - set WO_BOT_WXID manually.")
        return
    if wxid_row is None:
        typer.echo("")
        typer.echo("WARNING:  Bot messages found but their sender_wxid is NULL.")
        typer.echo("   Auto-discovery can't recover wxid; set WO_BOT_WXID manually.")
    else:
        typer.echo(f"discovered bot wxid: {wxid_row['sender_wxid']}")
        if not settings.bot_wxid:
            typer.echo(f"  consider setting WO_BOT_WXID={wxid_row['sender_wxid']} in .env")
            typer.echo("    (skips the wxid discovery delay on cold start)")
        elif settings.bot_wxid != wxid_row["sender_wxid"]:
            typer.echo(f"WARNING:  WO_BOT_WXID={settings.bot_wxid!r} but DB shows {wxid_row['sender_wxid']!r}")
    typer.echo("")
    typer.echo("recent bot messages:")
    from datetime import datetime
    for r in recent:
        ts = datetime.fromtimestamp(int(r["t"])).strftime("%Y-%m-%d %H:%M")
        body = (r["content_text"] or "").replace("\n", " ")[:60]
        typer.echo(f"  [{r['msg_id']}] {ts} ({r['group_name'] or '?'}): {body}")


@worker_app.command("mm")
def worker_mm() -> None:
    """Standalone OCR/ASR worker. Usually you don't need to run this - `ingest
    live` already starts an mm worker thread alongside SSE capture, so a
    normal "live + dispatcher" deployment covers it.

    Use this command when you want to run mm on its own - e.g. to drain a
    backfill queue without ingesting new messages, or to debug OCR/ASR in
    isolation. Long-running. Polls newest-first. Models lazy-load on first
    use: rapidocr-onnxruntime for images, faster-whisper (`small` by default;
    set WO_WHISPER_MODEL=tiny|base|medium|large-v3 to override) for voice.
    Both run locally on CPU - no data leaves the machine.
    """
    from .log_utils import setup_process_log
    from .worker.mm import run_mm_worker
    setup_process_log("mm")
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
    """Subscribe to WeFlow SSE for new messages in WO_GROUPS, AND run the mm
    OCR/ASR worker in a background thread. Requires WeFlow running.

    Combined process so a normal deployment is just two terminals:
    `ingest live` (this) + `dispatcher`. The mm thread shares the SQLite
    file via WAL - no separate process needed.
    """
    from .ingest.live import run_live
    from .log_utils import setup_process_log
    setup_process_log("live")
    run_live()


@app.command("dispatcher")
def dispatcher_cmd() -> None:
    """Watch DB for `@<bot> /find ...` commands; print results to stdout + log.

    Requires WO_BOT_NAME and WO_LLM_API_KEY in .env. Runs in foreground;
    Ctrl+C to stop. Safe to run alongside `ingest live`.
    """
    from .dispatcher import run_dispatcher
    from .log_utils import setup_process_log
    setup_process_log("dispatcher")
    run_dispatcher()


# --- agent memory inspection ----------------------------------------------


@agent_app.command("show")
def agent_show(
    group_id: str = typer.Argument(..., help="messages.group_id of the target group"),
) -> None:
    """Dump persona_drift + group_memory + lurk cursor for one group."""
    from datetime import datetime
    init_db()
    with get_conn() as conn:
        drift = conn.execute(
            "SELECT drift_text, updated_at, last_run_id FROM persona_drift WHERE group_id=?",
            (group_id,),
        ).fetchone()
        memory = conn.execute(
            "SELECT notes_text, size_chars, updated_at, last_run_id FROM group_memory WHERE group_id=?",
            (group_id,),
        ).fetchone()
        lurk_state = conn.execute(
            "SELECT last_msg_id, last_run_id, updated_at FROM agent_lurk_state WHERE group_id=?",
            (group_id,),
        ).fetchone()

    typer.echo(f"=== group_id={group_id!r} ===\n")
    typer.echo("--- persona_drift ---")
    if drift is None or not (drift["drift_text"] or "").strip():
        typer.echo("(empty)\n")
    else:
        ts = datetime.fromtimestamp(drift["updated_at"]).strftime("%Y-%m-%d %H:%M") if drift["updated_at"] else "?"
        typer.echo(f"updated_at={ts}  last_run_id={drift['last_run_id'] or '?'}")
        typer.echo(drift["drift_text"])
        typer.echo("")

    cap = settings.agent_memory_max_chars
    typer.echo(f"--- group_memory (cap {cap} chars) ---")
    if memory is None or not (memory["notes_text"] or "").strip():
        typer.echo("(empty)\n")
    else:
        ts = datetime.fromtimestamp(memory["updated_at"]).strftime("%Y-%m-%d %H:%M") if memory["updated_at"] else "?"
        size = memory["size_chars"] or 0
        pct = size * 100 // cap if cap else 0
        typer.echo(
            f"updated_at={ts}  last_run_id={memory['last_run_id'] or '?'}  "
            f"size={size} chars ({pct}% of cap)"
        )
        typer.echo(memory["notes_text"])

    typer.echo("\n--- lurk_state ---")
    if lurk_state is None:
        typer.echo("(no cursor yet)")
    else:
        ts = datetime.fromtimestamp(lurk_state["updated_at"]).strftime("%Y-%m-%d %H:%M") if lurk_state["updated_at"] else "?"
        typer.echo(
            f"last_msg_id={lurk_state['last_msg_id'] or '?'}  "
            f"last_run_id={lurk_state['last_run_id'] or '?'}  updated_at={ts}"
        )


@agent_app.command("wipe")
def agent_wipe(
    group_id: str = typer.Argument(..., help="messages.group_id of the target group"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    persona_only: bool = typer.Option(False, "--persona-only", help="Wipe persona_drift only, keep group_memory"),
    memory_only: bool = typer.Option(False, "--memory-only", help="Wipe group_memory only, keep persona_drift"),
) -> None:
    """Clear persona_drift and/or group_memory for one group. Destructive -
    bot resets to its defaults in this group. agent_run_log is NOT touched
    (audit trail stays).
    """
    if persona_only and memory_only:
        typer.echo("WARNING:  --persona-only and --memory-only are mutually exclusive")
        raise typer.Exit(1)
    targets = []
    if not memory_only:
        targets.append("persona_drift")
    if not persona_only:
        targets.append("group_memory")
    if not yes:
        typer.echo(f"about to clear {', '.join(targets)} for group_id={group_id!r}")
        confirm = typer.confirm("proceed?")
        if not confirm:
            typer.echo("aborted")
            raise typer.Exit(0)
    init_db()
    with get_conn() as conn:
        with transaction(conn):
            if "persona_drift" in targets:
                conn.execute("DELETE FROM persona_drift WHERE group_id=?", (group_id,))
            if "group_memory" in targets:
                conn.execute("DELETE FROM group_memory WHERE group_id=?", (group_id,))
    typer.echo(f"wiped: {', '.join(targets)}")


@agent_app.command("lurk")
def agent_lurk(
    group_id: str = typer.Argument(..., help="messages.group_id of the target group"),
) -> None:
    """One-shot 'lurk' run: bot reads new messages since its cursor, may
    search older history for context, and decides whether to update
    group_memory / persona_drift. No reply is ever sent to the group.

    Useful for periodic background memory consolidation (cron this every
    30 min for an active group), or to manually nudge the bot to update
    its impression of group activity after you've imported a backfill.
    """
    from .agent.orchestrator import chat_via_lurk
    from .dispatcher import _build_llm_client, _resolve_bot_wxid
    if not settings.bot_name:
        typer.echo("WARNING:  WO_BOT_NAME is empty - set it before lurking.")
        raise typer.Exit(1)
    init_db()
    settings.ensure_dirs()
    log_path = settings.data_dir / "dispatcher.log"
    llm_log_path = settings.data_dir / "llm_debug.log"
    llm = _build_llm_client()
    with get_conn() as conn:
        bot_wxid = _resolve_bot_wxid(conn, settings.bot_name)
        # Find the group_name for the prompt; OK if missing.
        row = conn.execute(
            "SELECT group_name FROM messages WHERE group_id=? AND group_name IS NOT NULL "
            "ORDER BY t DESC LIMIT 1",
            (group_id,),
        ).fetchone()
        group_name = row["group_name"] if row else None
        trace_block = chat_via_lurk(
            conn=conn,
            llm=llm,
            model=settings.llm_model,
            bot_name=settings.bot_name,
            bot_wxid=bot_wxid,
            group_id=group_id,
            group_name=group_name,
            log_path=log_path,
            llm_log_path=llm_log_path,
        )
    typer.echo(trace_block)


def _classify_silent(phase_a_trace: list[dict[str, object]] | None) -> tuple[str, str]:
    """Categorize why an agent run ended without a reply. Returns (label, detail).
    Labels: 'stay_silent' (A) / 'empty' (B) / 'max_steps' (C) / 'unknown'."""
    if not phase_a_trace:
        return "unknown", ""
    # A: explicit termination via stay_silent
    for s in phase_a_trace:
        if s.get("kind") == "terminate" and s.get("reason") == "stay_silent":
            # Reason given by the model lives in the prior tool_call args
            for prior in phase_a_trace:
                if prior.get("kind") == "tool_call" and prior.get("tool") == "stay_silent":
                    args = prior.get("args") or {}
                    return "stay_silent", str(args.get("reason", ""))[:80]
            return "stay_silent", ""
    # C: hit the step cap
    for s in phase_a_trace:
        if s.get("kind") == "max_steps_hit":
            return "max_steps", ""
    # B: a final step with empty content
    for s in phase_a_trace:
        if s.get("kind") == "final" and not s.get("content"):
            retried = any(t.get("kind") == "empty_final_retry" for t in phase_a_trace)
            return "empty", "(retried once)" if retried else ""
    return "unknown", ""


@agent_app.command("show-runs")
def agent_show_runs(
    group_id: str = typer.Argument(..., help="messages.group_id of the target group"),
    limit: int = typer.Option(10, "--limit", "-n"),
) -> None:
    """Recent agent_run_log entries with phase-B writes highlighted.

    Silent runs are tagged with one of three causes:
      stay_silent: model called the stay_silent tool (healthy decision)
      empty:       model returned empty final text without calling stay_silent
                   (likely confused / refused - investigate the trigger msg)
      max_steps:   model burned all tool-calling rounds without emitting text
                   (rare since runtime forces final on last step)
    """
    import json as _json
    from datetime import datetime
    from .agent.memory import list_recent_runs
    init_db()
    with get_conn() as conn:
        rows = list_recent_runs(conn, group_id, limit=limit)
    if not rows:
        typer.echo(f"no agent runs for group_id={group_id!r}")
        return
    for r in rows:
        ts = datetime.fromtimestamp(r["started_at"]).strftime("%Y-%m-%d %H:%M:%S") if r["started_at"] else "?"
        dur = (r["finished_at"] - r["started_at"]) if (r["started_at"] and r["finished_at"]) else 0
        reply_text = r["reply_text"]
        if r["trigger_kind"] == "lurk":
            reply = "(lurk: no chat reply)"
        elif reply_text is None or not reply_text.strip():
            try:
                pa = _json.loads(r["phase_a_trace"] or "[]")
            except _json.JSONDecodeError:
                pa = []
            label, detail = _classify_silent(pa)
            reply = f"(silent: {label}{' - ' + detail if detail else ''})"
        else:
            reply = reply_text.replace("\n", " ")[:80]
        typer.echo(
            f"[{r['run_id']}] {ts}  trigger={r['trigger_kind']:12s}  "
            f"msg_id={r['trigger_msg_id'] or '?':>7}  {dur:.1f}s"
        )
        typer.echo(f"     reply: {reply}")
        # Surface any Phase B writes inline
        try:
            pb = _json.loads(r["phase_b_trace"] or "[]")
        except _json.JSONDecodeError:
            pb = []
        writes = [s for s in pb if s.get("kind") == "tool_call" and s.get("tool", "").startswith("update_")]
        for w in writes:
            args = w.get("args", {})
            preview_key = "drift_text" if w["tool"] == "update_persona_drift" else "notes_text"
            preview = (args.get(preview_key, "") or "").replace("\n", " ")[:80]
            typer.echo(f"     phase B {w['tool']}: {preview}")


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


@openclaw_app.command("mcp-test")
def openclaw_mcp_test() -> None:
    """End-to-end smoke test: spawn `mcp-serve` as a subprocess, do the MCP
    initialize handshake over stdio, list tools, then call search_group_messages
    on the busiest group. This is what OpenClaw will do under the hood - if
    THIS works and OpenClaw still doesn't see the tools, the bug is on
    OpenClaw's side (registration/profile/restart), not ours.
    """
    import asyncio
    import json as _json
    import os
    import sys
    from mcp import ClientSession  # type: ignore[import-untyped]
    from mcp.client.stdio import StdioServerParameters, stdio_client  # type: ignore[import-untyped]

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

    # Resolve a real group_id BEFORE we start the async session, so database
    # access errors surface with a normal traceback instead of getting buried
    # inside an asyncio TaskGroup ExceptionGroup.
    typer.echo("[0/4] open SQLite ...")
    try:
        with get_conn() as conn:
            db_row = conn.execute(
                "SELECT group_id FROM messages WHERE group_id IS NOT NULL "
                "GROUP BY group_id ORDER BY COUNT(*) DESC LIMIT 1"
            ).fetchone()
            img_row = conn.execute(
                "SELECT msg_id, group_id FROM messages "
                "WHERE type='image' AND media_path IS NOT NULL "
                "ORDER BY msg_id DESC LIMIT 1"
            ).fetchone()
    except Exception as e:
        typer.echo(f"      [ERR] cannot open/read DB at {settings.db_path}: {type(e).__name__}: {e}")
        typer.echo("      Check that the MCP server cwd points at this checkout and that the DB is not locked.")
        raise typer.Exit(2)
    test_gid = db_row["group_id"] if db_row else None
    typer.echo(f"      OK; busiest group_id={test_gid!r}")
    if img_row is not None:
        typer.echo(f"      latest image row: msg_id={img_row['msg_id']} group_id={img_row['group_id']!r}")

    async def run() -> None:
        # mcp.client.stdio does NOT inherit parent env by default, so pass it
        # through explicitly for PATH/HOME/etc. used by the spawned `uv run`.
        params = StdioServerParameters(
            command="uv",
            args=["run", "wechat-oracle", "openclaw", "mcp-serve"],
            env=dict(os.environ),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                typer.echo("[1/4] initialize ...")
                init = await session.initialize()
                typer.echo(f"      server: {init.serverInfo.name} v{init.serverInfo.version}")
                typer.echo(f"      protocol: {init.protocolVersion}")

                typer.echo("[2/4] list tools (with raw schemas) ...")
                tools_resp = await session.list_tools()
                typer.echo(f"      got {len(tools_resp.tools)} tools.")
                for t in tools_resp.tools:
                    in_schema = getattr(t, "inputSchema", None)
                    out_schema = getattr(t, "outputSchema", None)
                    typer.echo(f"      -- {t.name} --")
                    in_str = _json.dumps(in_schema, ensure_ascii=False) if in_schema else "(none)"
                    out_str = _json.dumps(out_schema, ensure_ascii=False) if out_schema else "(none)"
                    typer.echo(f"        inputSchema:  {in_str[:300]}")
                    typer.echo(f"        outputSchema: {out_str[:300]}")

                if test_gid is None:
                    typer.echo("[3/4] skip search_group_messages call - no groups in DB")
                else:
                    typer.echo(f"[3/4] search_group_messages(group_id={test_gid!r}, query='', limit=2) ...")
                    result = await session.call_tool(
                        "search_group_messages",
                        {"group_id": test_gid, "query": "", "limit": 2},
                    )
                    for c in result.content:
                        text = getattr(c, "text", None)
                        if text:
                            typer.echo("      " + text.replace("\n", "\n      ")[:300])

                if img_row is None:
                    typer.echo("[4/4] skip load_image - no image rows with media_path in DB")
                else:
                    typer.echo(
                        f"[4/4] load_image(group_id={img_row['group_id']!r}, "
                        f"msg_id={img_row['msg_id']}) ..."
                    )
                    img_result = await session.call_tool(
                        "load_image",
                        {"group_id": img_row["group_id"], "msg_id": int(img_row["msg_id"])},
                    )
                    typer.echo(f"      isError={getattr(img_result, 'isError', None)}")
                    typer.echo(f"      content blocks: {len(img_result.content)}")
                    for i, c in enumerate(img_result.content):
                        kind = type(c).__name__
                        bits = []
                        for attr in ("type", "mimeType"):
                            if hasattr(c, attr):
                                bits.append(f"{attr}={getattr(c, attr)!r}")
                        if hasattr(c, "data"):
                            d = c.data
                            bits.append(f"data_len={len(d) if isinstance(d, str) else '?'}")
                        if hasattr(c, "text"):
                            bits.append(f"text={c.text[:80]!r}")
                        typer.echo(f"      content[{i}]: {kind}  " + "  ".join(bits))
        typer.echo("\n[OK] our MCP server works end-to-end via stdio.")

    try:
        asyncio.run(run())
    except BaseExceptionGroup as eg:  # type: ignore[name-defined]  # 3.11+
        typer.echo(f"\n[ERR] ExceptionGroup of {len(eg.exceptions)} sub-exception(s):")
        for i, sub in enumerate(eg.exceptions, 1):
            typer.echo(f"  ({i}) {type(sub).__name__}: {sub}")
            inner = getattr(sub, "exceptions", None)
            if inner:
                for j, deeper in enumerate(inner, 1):
                    typer.echo(f"      .{j} {type(deeper).__name__}: {deeper}")
        raise typer.Exit(2)
    except Exception as e:
        typer.echo(f"\n[ERR] {type(e).__name__}: {e}")
        raise typer.Exit(2)


@openclaw_app.command("mcp-serve")
def openclaw_mcp_serve() -> None:
    """Start the MCP server that exposes WeChat-Oracle tools to OpenClaw's
    wechat-bot agent. Stdio-based - meant to be spawned by OpenClaw's MCP
    client. Register on the OpenClaw side roughly like:

      openclaw mcp set wechat-oracle \\
        --command "uv" --args "run wechat-oracle openclaw mcp-serve"

    Exposes the full OpenClaw tool surface: history search, quote/forward
    expansion, media reads, and memory/persona read-write tools.
    """
    from .mcp_server import run_mcp_server
    run_mcp_server()


@openclaw_app.command("ping")
def openclaw_ping(
    message: str = typer.Argument("ping", help="Text to send"),
    timeout: float = typer.Option(120.0, "--timeout", "-t", help="HTTP timeout in seconds"),
) -> None:
    """Smoke-test the OpenClaw chat-completions endpoint with the configured
    agent. Prints HTTP status, latency, and the assistant reply (or error).

    Note: a real wechat-bot agent with full skills loaded can have prompt
    sizes in the 10-50K range. First call typically takes 20-40s. Default
    timeout is 120s; raise with --timeout if your agent is heavier.
    """
    import time as _time
    import sys as _sys
    import httpx
    if not settings.openclaw_token:
        typer.echo("WARNING:  WO_OPENCLAW_TOKEN is empty - set it to the gateway's auth token.")
        raise typer.Exit(1)
    url = f"{settings.openclaw_gateway_url.rstrip('/')}/v1/chat/completions"
    payload = {
        "model": f"openclaw/{settings.openclaw_agent_id}",
        "messages": [{"role": "user", "content": message}],
    }
    headers = {"Authorization": f"Bearer {settings.openclaw_token}"}
    typer.echo(f"POST {url}  model={payload['model']!r}")
    typer.echo(f"(waiting up to {timeout:.0f}s for response - first call to a heavy agent can take 20-40s)")
    _sys.stdout.flush()
    t0 = _time.time()
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    except httpx.HTTPError as e:
        typer.echo(f"[ERR] HTTP error after {_time.time() - t0:.1f}s: {e}")
        raise typer.Exit(2)
    dt = _time.time() - t0
    typer.echo(f"HTTP {resp.status_code}  dt={dt:.2f}s")
    if resp.status_code != 200:
        typer.echo(f"body: {resp.text[:600]}")
        raise typer.Exit(2)
    body = resp.json()
    choices = body.get("choices") or []
    if not choices:
        typer.echo(f"WARNING:  no choices in response: {body}")
        raise typer.Exit(2)
    reply = (choices[0].get("message") or {}).get("content") or ""
    usage = body.get("usage") or {}
    typer.echo(f"reply: {reply!r}")
    if usage:
        typer.echo(f"usage: {usage}")


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
