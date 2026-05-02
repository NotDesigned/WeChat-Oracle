#!/usr/bin/env python3
"""PostToolUse hook: enforce CLAUDE.md drift contracts.

Watches edits to several "spec" files. If a spec changed in a way that
encodes a fact also stored elsewhere, require the paired files to also be
dirty in the working tree (vs HEAD). Emits a system reminder via stderr
(exit 2) so Claude course-corrects.

Each rule is a small dataclass: which file's edit triggers it, what marker
in the diff signals a contract change, and which paired files must show
dirty state. Rules are independent; multiple may fire for one edit.

This is a backstop, not a gatekeeper — an agent can suppress it by editing
both files (which is the desired outcome) or by stashing/reordering work.
The CLAUDE.md "易漂移点速查" table is the normative source; this hook just
tries to catch the easy misses.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Rule:
    """One drift-prevention rule.

    `trigger_suffix`: edited file ends with this (forward slashes; we
        normalize backslashes from Windows paths first).
    `markers`: regex patterns; if ANY appears in a `+` line of trigger_file's
        diff vs HEAD, the contract is considered changed. Empty tuple means
        any non-trivial diff triggers.
    `require_any_dirty`: at least one of these paths (relative to repo root)
        must have a non-empty `git diff HEAD` for the contract to be
        considered honored.
    `message`: what we tell Claude when the rule fires.
    """
    trigger_suffix: str
    markers: tuple[re.Pattern, ...]
    require_any_dirty: tuple[str, ...]
    message: str


# --- markers ---------------------------------------------------------------

# A `+` line in the trigger file's diff matches one of these → contract change.

# dispatcher.py: command-class contract (matches existing hook behavior).
_DISPATCHER_MARKERS = (
    re.compile(r"\bname\s*="),
    re.compile(r"\busage\s*="),
    re.compile(r"\bdescription\s*="),
    re.compile(r"\bexamples\s*="),
    re.compile(r"@register"),
    re.compile(r"\bclass\s+\w+Command\b"),
    re.compile(r"\bdef\s+parse\("),
    re.compile(r"\bparse_command\("),
)

# schema.sql: any DDL change. Comment-only edits don't fire.
_SCHEMA_MARKERS = (
    re.compile(r"\bCREATE\s+TABLE\b", re.I),
    re.compile(r"\bALTER\s+TABLE\b", re.I),
    re.compile(r"\bDROP\s+TABLE\b", re.I),
    re.compile(r"\bCREATE\s+INDEX\b", re.I),
    # Column declarations: indented identifier followed by SQL type token.
    re.compile(r"^\s*\w+\s+(INTEGER|TEXT|REAL|BLOB|NUMERIC|BOOLEAN)\b", re.I | re.M),
)

# config.py: a Settings field declaration changed. Heuristic: a + line that
# looks like an indented type-annotated assignment (`field: type = ...`).
# False positives in method bodies are tolerable.
_CONFIG_MARKERS = (
    re.compile(r"^\s+\w+:\s+\S+\s*=", re.M),
    re.compile(r"\bField\s*\("),
    re.compile(r"\bSettingsConfigDict\b"),
)

# cli.py: typer command surface changed.
_CLI_MARKERS = (
    re.compile(r"@\w+\.command\b"),
    re.compile(r"@\w+\.callback\b"),
    re.compile(r"\.add_typer\("),
)


# --- rule table -----------------------------------------------------------

RULES: tuple[Rule, ...] = (
    Rule(
        trigger_suffix="src/wechat_oracle/dispatcher.py",
        markers=_DISPATCHER_MARKERS,
        require_any_dirty=("README.md",),
        message=(
            "dispatcher.py 命令契约行（usage/examples/description/name/@register/"
            "class ...Command/def parse/parse_command）有改动，但 README.md 未同步。"
            "按 CLAUDE.md「命令体系维护契约」必须同 PR 修 README「命令详解」段。"
        ),
    ),
    Rule(
        trigger_suffix="src/wechat_oracle/schema.sql",
        markers=_SCHEMA_MARKERS,
        require_any_dirty=("README.md", "src/wechat_oracle/models.py", "src/wechat_oracle/ingest/writer.py"),
        message=(
            "schema.sql DDL 有改动，但 README.md / models.py / writer.py 都没动。"
            "按 CLAUDE.md「易漂移点 F1/F2」schema 是四处冗余存储（DDL / Pydantic / "
            "INSERT_SQL / README 数据库段），至少要同步其中一个。"
        ),
    ),
    Rule(
        trigger_suffix="src/wechat_oracle/config.py",
        markers=_CONFIG_MARKERS,
        require_any_dirty=("README.md",),
        message=(
            "config.py 的 Settings 字段（或默认值 / Field 调用 / 模型配置）有改动，"
            "但 README.md 未同步。按 CLAUDE.md「易漂移点 F3」必须同步「配置参考」表"
            "和（如适用）快速上手的 .env 示例。"
        ),
    ),
    Rule(
        trigger_suffix="src/wechat_oracle/cli.py",
        markers=_CLI_MARKERS,
        require_any_dirty=("README.md",),
        message=(
            "cli.py 的 typer 命令面有改动（@app.command / add_typer / @app.callback），"
            "但 README.md 未同步。按 CLAUDE.md「易漂移点 F5」必须同步「快速上手」段和「三进程」表。"
        ),
    ),
)


# --- runner ----------------------------------------------------------------

def _git_diff(repo: Path, path: str) -> str:
    """Diff `path` against HEAD. Force UTF-8 decode (Windows default cp936
    chokes on Chinese in our diffs); replace undecodable bytes so we never
    return None and crash downstream regex.
    """
    res = subprocess.run(
        ["git", "-C", str(repo), "diff", "HEAD", "--", path],
        capture_output=True, encoding="utf-8", errors="replace",
    )
    return res.stdout or ""


def _added_lines(diff: str) -> list[str]:
    """Return only the `+` lines (excluding the `+++` header)."""
    out = []
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            out.append(line[1:])
    return out


def _markers_hit(added: list[str], markers: tuple[re.Pattern, ...]) -> bool:
    if not markers:
        return bool(added)  # empty marker tuple → any addition counts
    blob = "\n".join(added)
    return any(p.search(blob) for p in markers)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0

    fp = ((data.get("tool_input") or {}).get("file_path") or "").replace("\\", "/")
    if not fp:
        return 0

    repo = Path(__file__).resolve().parents[2]

    fired_messages: list[str] = []
    for rule in RULES:
        if not fp.endswith(rule.trigger_suffix):
            continue
        diff = _git_diff(repo, rule.trigger_suffix)
        added = _added_lines(diff)
        if not _markers_hit(added, rule.markers):
            continue
        if any(_git_diff(repo, p).strip() for p in rule.require_any_dirty):
            continue  # at least one paired file is dirty → contract honored
        fired_messages.append(
            f"{rule.message}\n  - 同步目标（任一即可）: {', '.join(rule.require_any_dirty)}"
        )

    if fired_messages:
        sys.stderr.write("文档同步契约提醒：\n")
        for msg in fired_messages:
            sys.stderr.write(f"\n• {msg}\n")
        sys.stderr.write(
            "\n查 CLAUDE.md「易漂移点速查」段了解全部冗余存储位置。"
            "hook 只是 backstop，最终一致性以契约为准。\n"
        )
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
