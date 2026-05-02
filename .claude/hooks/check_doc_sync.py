#!/usr/bin/env python3
"""Doc-sync hook: enforce CLAUDE.md drift contracts.

Two run modes share the same rule table:

  PostToolUse (default)
      Reads JSON from stdin (Claude Code passes tool_use info), checks
      against `git diff HEAD` (working tree). Exit 2 surfaces stderr to
      Claude as a system reminder so it course-corrects mid-session.

  --pre-commit
      Iterates over `git diff --cached --name-only` (staged-only view),
      checks against `git diff --cached` for each rule. Exit 1 aborts
      `git commit`. Use as a backstop when the PostToolUse hook didn't
      fire (manual edit, rebase, different agent, etc.).

Each rule: which trigger file, which `+`-line markers signal a contract
change, which paired files must also be dirty (working) or staged
(pre-commit) for the rule to be satisfied.

This is a backstop, not a gatekeeper — agents/users can comply by editing
the paired file, or bypass with --no-verify. CLAUDE.md「易漂移点速查」is
the normative source; this hook just catches the easy misses.
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

def _git_diff(repo: Path, path: str, *, staged: bool = False) -> str:
    """Diff `path`. `staged=True` → `git diff --cached --` (only staged
    changes, used by pre-commit). `staged=False` → `git diff HEAD --`
    (working tree + staged, used by PostToolUse).

    UTF-8 decode forced; Windows default cp936 chokes on Chinese in diffs.
    Errors replaced rather than raised so downstream regex never sees None.
    """
    cmd = ["git", "-C", str(repo), "diff"]
    if staged:
        cmd.append("--cached")
    else:
        cmd.append("HEAD")
    cmd.extend(["--", path])
    res = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace")
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


def _check_one_path(repo: Path, fp: str, *, staged: bool) -> list[str]:
    """Run all matching rules for a single edited path. Returns list of
    fired-rule messages (empty = no violation)."""
    fired: list[str] = []
    for rule in RULES:
        if not fp.endswith(rule.trigger_suffix):
            continue
        diff = _git_diff(repo, rule.trigger_suffix, staged=staged)
        added = _added_lines(diff)
        if not _markers_hit(added, rule.markers):
            continue
        if any(_git_diff(repo, p, staged=staged).strip() for p in rule.require_any_dirty):
            continue
        fired.append(
            f"{rule.message}\n  - 同步目标（任一即可）: {', '.join(rule.require_any_dirty)}"
        )
    return fired


def _emit(fired: list[str], stream) -> None:
    """Write fired-rule messages to a stream with consistent formatting."""
    if not fired:
        return
    stream.write("文档同步契约提醒：\n")
    for msg in fired:
        stream.write(f"\n• {msg}\n")
    stream.write(
        "\n查 CLAUDE.md「易漂移点速查」段了解全部冗余存储位置。"
        "hook 只是 backstop，最终一致性以契约为准。\n"
    )


def main_post_tool() -> int:
    """PostToolUse mode: read tool_input JSON from stdin, check working tree."""
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    fp = ((data.get("tool_input") or {}).get("file_path") or "").replace("\\", "/")
    if not fp:
        return 0
    repo = Path(__file__).resolve().parents[2]
    fired = _check_one_path(repo, fp, staged=False)
    _emit(fired, sys.stderr)
    return 2 if fired else 0


def main_pre_commit() -> int:
    """Pre-commit mode: scan all staged paths against staged-only diffs."""
    repo = Path(__file__).resolve().parents[2]
    res = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--name-only"],
        capture_output=True, encoding="utf-8", errors="replace",
    )
    staged_paths = [p.replace("\\", "/") for p in (res.stdout or "").splitlines() if p]
    fired: list[str] = []
    for fp in staged_paths:
        fired.extend(_check_one_path(repo, fp, staged=True))
    _emit(fired, sys.stderr)
    return 1 if fired else 0


if __name__ == "__main__":
    if "--pre-commit" in sys.argv:
        sys.exit(main_pre_commit())
    sys.exit(main_post_tool())
