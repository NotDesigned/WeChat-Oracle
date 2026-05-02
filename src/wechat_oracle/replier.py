"""Reply backends for the dispatcher.

The dispatcher generates a text reply, then needs to put it back into the
WeChat conversation. Three backends:

  - `Wx4pyReplier`   — default. Drives WeChat's UI via wx4py (Windows only;
    requires main window visible). Identifies target groups by display
    name. Battle-tested for our flow.
  - `OpenclawReplier` — experimental. HTTP API via openclaw.py. Cross-
    platform but: (a) needs a separate bot account login, (b) group send
    UNCONFIRMED at time of writing — see openclaw.py module docstring.
    Looks up group_id from `<data_dir>/openclaw-groups.json` mapping.
  - `StdoutReplier`  — no-op fallback. Prints to logs only. Used when
    WO_REPLY=False or any backend fails to initialize.

`build_replier()` is the single factory call from the dispatcher. It reads
`settings.reply_backend` and tries the chosen backend; on failure (no token,
wx4py can't connect, etc.) it warns and degrades to StdoutReplier so the
dispatcher loop still runs.

Adding a backend: implement the `Replier` Protocol (just `send` and
`disconnect`) and add a branch in `build_replier()`. No dispatcher change
needed — that's the whole point of this file.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from loguru import logger

from .config import settings


# Four-per-em space — WeChat's @-mention separator.
_AT_SEP = " "


class Replier(Protocol):
    """The dispatcher only needs these two ops."""

    def send(self, group_name: str | None, requester: str | None, text: str) -> None: ...
    def disconnect(self) -> None: ...


# ---- stdout (always-available fallback) -----------------------------------


class StdoutReplier:
    """Drop messages on the floor (after they've been logged elsewhere).
    Used when WO_REPLY=False, or as a graceful degradation when the chosen
    backend can't connect."""

    def send(self, group_name: str | None, requester: str | None, text: str) -> None:
        logger.debug("stdout-replier: would send to {}: {!r}", group_name, text[:80])

    def disconnect(self) -> None:
        pass


# ---- wx4py (current default) ----------------------------------------------


class Wx4pyReplier:
    """UI-automation backend. `_wx` is the connected wx4py.WeChatClient.
    Cooperates with our `WO_BOT_NAME` invariant: the dispatcher already
    verified at startup that wx4py's group_nickname matches WO_BOT_NAME for
    every watched group."""

    def __init__(self, wx) -> None:
        self._wx = wx

    def send(self, group_name: str | None, requester: str | None, text: str) -> None:
        if not group_name:
            return
        body = f"@{requester}{_AT_SEP}{text}" if requester else text
        try:
            self._wx.chat_window.send_to(group_name, body, target_type="group")
        except Exception as e:
            logger.warning("wx4py send_to failed (group={!r}): {}", group_name, e)

    def disconnect(self) -> None:
        try:
            self._wx.disconnect()
        except Exception as e:
            logger.warning("wx4py disconnect failed: {}", e)

    @classmethod
    def try_connect(cls) -> Replier | None:
        """Returns a connected Wx4pyReplier or None if wx4py is unhappy.
        Caller decides whether to fall back to stdout."""
        try:
            from wx4py import WeChatClient
        except ImportError:
            logger.warning("wx4py not installed; can't use wx4py backend")
            return None
        try:
            wx = WeChatClient()
            wx.connect()
        except Exception as e:
            logger.warning(
                "wx4py connect failed ({}); replies disabled this run. "
                "Open WeChat's main window (not in tray) and restart dispatcher.", e,
            )
            return None

        # Per-group identity check: warn (don't block) if logged-in account's
        # group_nickname doesn't match WO_BOT_NAME — usually means wrong account.
        for group_name in settings.groups:
            try:
                actual = wx.group_manager.get_group_nickname(group_name)
            except Exception:
                continue
            if actual and actual != settings.bot_name:
                logger.warning(
                    "wx4py: in group {!r} the logged-in account's nickname is {!r}, "
                    "but WO_BOT_NAME={!r}. Did you log into the wrong account?",
                    group_name, actual, settings.bot_name,
                )
        return cls(wx)


# ---- openclaw (experimental) ----------------------------------------------


class OpenclawReplier:
    """HTTP backend via Tencent's iLink Bot API.

    Group routing: needs a `<data_dir>/openclaw-groups.json` mapping of
    `{display_name: openclaw_group_id}`, populated manually from a `probe`
    session (use the `wechat-oracle openclaw probe` CLI command to discover
    group_ids). Without an entry for `group_name`, send is a no-op.

    UNCONFIRMED at time of writing whether group send actually works. If it
    doesn't, the openclaw API rejects with some error; we log and continue.
    """

    def __init__(self, client, group_map: dict[str, str]) -> None:
        self._client = client
        self._group_map = group_map

    def send(self, group_name: str | None, requester: str | None, text: str) -> None:
        if not group_name:
            return
        group_id = self._group_map.get(group_name)
        if not group_id:
            logger.warning(
                "openclaw: no group_id mapping for {!r}; "
                "add it to {}/openclaw-groups.json after a probe session",
                group_name, settings.data_dir,
            )
            return
        body = f"@{requester}{_AT_SEP}{text}" if requester else text
        try:
            self._client.send_text(group_id=group_id, text=body)
        except Exception as e:
            logger.warning(
                "openclaw send failed (group={!r} group_id={!r}): {}",
                group_name, group_id, e,
            )

    def disconnect(self) -> None:
        self._client.close()

    @classmethod
    def try_connect(cls, data_dir: Path) -> Replier | None:
        from .openclaw import OpenclawClient, OpenclawSession
        token_path = data_dir / "openclaw-token.json"
        session = OpenclawSession.from_json(token_path)
        if not session:
            logger.warning(
                "openclaw: no token at {} — run `wechat-oracle openclaw login` first",
                token_path,
            )
            return None
        groups_path = data_dir / "openclaw-groups.json"
        group_map: dict[str, str] = {}
        if groups_path.exists():
            try:
                group_map = json.loads(groups_path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("openclaw: bad {}: {}; sending will be no-op", groups_path, e)
        else:
            logger.warning(
                "openclaw: no group mapping at {}; "
                "run `wechat-oracle openclaw probe` to discover group_ids", groups_path,
            )
        client = OpenclawClient(session)
        logger.info("openclaw: connected as bot_id={!r}, {} group(s) mapped",
                    session.bot_id, len(group_map))
        return cls(client, group_map)


# ---- factory --------------------------------------------------------------


def build_replier() -> Replier:
    """Build the configured replier. Always returns a working Replier (may
    be StdoutReplier if backend init failed)."""
    if not settings.reply:
        logger.info("WO_REPLY=False; using stdout replier")
        return StdoutReplier()

    backend = (settings.reply_backend or "wx4py").lower()
    if backend == "stdout":
        return StdoutReplier()
    if backend == "wx4py":
        replier = Wx4pyReplier.try_connect()
        return replier or StdoutReplier()
    if backend == "openclaw":
        replier = OpenclawReplier.try_connect(settings.data_dir)
        return replier or StdoutReplier()

    logger.warning("unknown WO_REPLY_BACKEND={!r}; using stdout", backend)
    return StdoutReplier()
